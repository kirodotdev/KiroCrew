// The page-side half of the `browser` MCP tool: ONE script, evaluated in the
// agent's tab through the Harness `js` helper, that installs `window.__kcWalker`
// once per document and returns it. `browser_harness/ops.py` appends a method
// call to this source (`<source>.snapshot()`), so every page-side read and
// write the tool performs is a method on the object this file builds. Nothing
// else is ever evaluated in the page.
//
// Moved from `website/electron/browser-ops.js` (the native panel's walker) when
// browsing moved to Browser Harness. The ref layer is OURS: `snapshot` mints
// `eN` refs into `window.__kcRefs` and the other methods resolve them back to
// live elements. Refs live as long as the Document object: a real navigation
// swaps the document (and this whole realm), an SPA route change does not.
//
// Every method returns a JSON-serialisable value. A refusal is a return value
// (`{ok:false, code}`), never a throw, so Python can tell "the page said no"
// from a transport failure.
//
// `observe`, `check` and the element table they share follow the snapshot of
// browser-use/jev-ultrafast (jev_ultrafast/snapshot.js, MIT, Copyright (c) 2026
// Browser Use; see NOTICE): one atomic read of the visible controls and text, an
// identity per real node (here: our `eN` refs), a scoped guard per element, and a
// re-check of the target's geometry and occlusion just before input.
(() => {
  var NAME_CAP = 120;
  var CONSOLE_CAP = 500;
  // One console entry's text; a page that logs a megabyte string must not make the
  // worker's reply line a megabyte. The gateway caps the whole outline at 20,000
  // characters, so a snapshot past NODE_CAP nodes carries nothing it would show.
  var CONSOLE_TEXT_CAP = 2000;
  var NODE_CAP = 1500;
  // `glance` bounds: elements looked at, entries per list, and the longest name or
  // message read. Text past its bound is left out, never cut, so a credential
  // reaches the gateway's redaction whole. The text bounds are four times the
  // report's display caps (`change_report.NAME_CAP`, `TEXT_CAP`); the gateway
  // redacts first and only then cuts for display.
  var GLANCE_SCAN_CAP = 600;
  var GLANCE_LIST_CAP = 10;
  var GLANCE_NAME_CAP = 240;
  var GLANCE_TEXT_CAP = 640;
  // Most ids an `aria-labelledby` list may name for `glance` to read it.
  var GLANCE_ID_CAP = 4;
  // Cross-origin frames one snapshot hands the gateway to read (ops.py FRAME_READ_CAP).
  var FRAME_SLOT_CAP = 8;
  var INTERACTIVE_TAGS = { a: true, button: true, input: true, select: true, textarea: true };
  var INTERACTIVE_ROLES = {
    button: 1, link: 1, checkbox: 1, radio: 1, tab: 1, menuitem: 1, option: 1,
    "switch": 1, combobox: 1, textbox: 1
  };
  var SECRET_NAME_RE = /(^|[^a-z])(password|passwd|pwd|otp|totp|mfa|cvv|cvc|secret|token|apikey)([^a-z]|$)/i;
  // `autocomplete` tokens that name a secret field whatever it is called.
  // Kept equal to `dom_census.SECRET_AUTOCOMPLETE` by a test.
  var SECRET_AUTOCOMPLETE = ["current-password", "new-password", "one-time-code", "cc-csc"];

  // Whether char code *c* is white space as a regular expression's `\s` counts it:
  // tab, line feed, vertical tab, form feed, carriage return, space, no-break
  // space, ogham space, the en and em spaces to the hair space, the line and
  // paragraph separators, narrow no-break space, medium mathematical space,
  // ideographic space and the byte order mark.
  function isSpace(c) {
    return c === 32 || (c >= 9 && c <= 13) || c === 160 || c === 5760 ||
      (c >= 8192 && c <= 8202) || c === 8232 || c === 8233 || c === 8239 ||
      c === 8287 || c === 12288 || c === 65279;
  }
  // *s* with each run of white space made one space and both ends trimmed. The
  // text is rebuilt whole, so a credential in it is never split.
  function squashSpace(s) {
    var out = "", gap = false;
    for (var i = 0; i < s.length; i++) {
      if (isSpace(s.charCodeAt(i))) { gap = true; continue; }
      if (gap && out) out += " ";
      gap = false;
      out += s.charAt(i);
    }
    return out;
  }
  // The words of *s*, split at runs of white space, with no empty word.
  function spaceWords(s) {
    var words = [], word = "";
    for (var i = 0; i < s.length; i++) {
      if (isSpace(s.charCodeAt(i))) {
        if (word) words.push(word);
        word = "";
      } else {
        word += s.charAt(i);
      }
    }
    if (word) words.push(word);
    return words;
  }

  // A camelCase name split into words (`otpCode` -> `otp Code`, `OTPCode` ->
  // `OTP Code`), so the name rule's word boundaries see `otp` as a word. The rule
  // is tested on the name as written too: `apiKey` splits to `api Key`.
  function splitCamel(s) {
    return s.replace(/([A-Z]+)([A-Z][a-z])/g, "$1 $2").replace(/([a-z0-9])([A-Z])/g, "$1 $2");
  }

  if (window.__kcRefDocObj !== document || !(window.__kcRefs instanceof Map)) {
    window.__kcRefs = new Map();
    window.__kcRefSeq = 0;
    window.__kcRefDocObj = document;
  }
  var refs = window.__kcRefs;
  // Refs are strong references; drop the ones whose element left the DOM so an
  // SPA that re-renders for hours does not pin every element ever walked.
  refs.forEach(function (el, r) { if (!el || !el.isConnected) refs.delete(r); });

  function visible(el) {
    var style = window.getComputedStyle(el);
    if (!style) return false;
    if (style.display === "none" || style.visibility === "hidden") return false;
    var rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return false;
    // `display: none` up the tree. checkVisibility() reads the flat tree; the
    // offsetParent test does not cross a shadow root, so content slotted into a
    // web component's fixed dialog (Alaska's fs-auro-dialog) read as hidden.
    if (typeof el.checkVisibility === "function") return el.checkVisibility();
    if (el.offsetParent === null && style.position !== "fixed") return false;
    return true;
  }

  // Tags whose text is never part of a name: not drawn, or a form field whose
  // text is the field's content (a hidden prefilled <textarea> slotted into a
  // button must not name it). aria-hidden text is left out of a name too.
  var NAMELESS_TAGS = {
    script: 1, style: 1, template: 1, input: 1, textarea: 1, select: 1, option: 1
  };

  // Text as drawn, through <slot>s: a web component's inner <button> holds a
  // <slot>, and its label is the host's light-DOM text (Alaska's auro-button),
  // which the button's own textContent never contains. Every element is checked
  // here, slots and slotted ones too, so hidden, field and secret-field text adds
  // nothing. A display:contents wrapper (a slot by default) draws no box of its
  // own, so only its style decides, not checkVisibility(). Pieces join with no
  // separator, as textContent joins them: "$1<span>.99</span>" reads "$1.99".
  function drawnText(node, depth) {
    if (node.nodeType === 3) return node.nodeValue;
    if (node.nodeType !== 1 || depth > 16) return "";
    var tag = node.tagName.toLowerCase();
    if (NAMELESS_TAGS[tag] || secretish(node)) return "";
    if (node.getAttribute("aria-hidden") === "true") return "";
    var style = window.getComputedStyle(node);
    if (!style || style.display === "none" || style.visibility === "hidden") return "";
    if (tag === "slot" && typeof node.assignedNodes === "function") {
      return joinText(node.assignedNodes({ flatten: true }), depth);
    }
    if (style.display !== "contents" && typeof node.checkVisibility === "function" &&
        !node.checkVisibility()) return "";
    return joinText(node.childNodes || [], depth);
  }

  function joinText(nodes, depth) {
    var s = "";
    for (var i = 0; i < nodes.length; i++) s += drawnText(nodes[i], depth + 1);
    return s;
  }

  // Why a quick ask would refuse this password input now ("disabled", "hidden",
  // "covered"), or "" when it could bind it, by the ask's own rule (quick_ask.py
  // `_INSPECT_JS` + `target_problem`): a box of at least 2 x 2, not hidden or
  // faded to <= 0.05 up the composed tree, enabled, writable, and at its centre
  // only itself or its own label. An identifier-first page keeps the next step's
  // password field in the DOM already hidden, disabled or covered; given a ref,
  // the agent asked for it and was refused. A field the agent can scroll to (out
  // of the window, or out of its scroll box's view) keeps its ref; one no scroll
  // brings in (fixed in place, or before the page's start) is "hidden".
  function unaskable(el) {
    if (el.disabled === true || el.readOnly === true || el.getAttribute("aria-disabled") === "true") return "disabled";
    // A same-origin frame's field: its own window's viewport and styles.
    var view = (el.ownerDocument && el.ownerDocument.defaultView) || window;
    var r = el.getBoundingClientRect();
    var cs = view.getComputedStyle(el);
    var fop = function (s) {
      var m = /opacity\(\s*([0-9.]+)(%?)\s*\)/.exec(s.filter || "");
      return m ? parseFloat(m[1]) / (m[2] ? 100 : 1) : 1;
    };
    if (r.width < 2 || r.height < 2 || cs.display === "none" || cs.visibility === "hidden"
        || parseFloat(cs.opacity || "1") * fop(cs) <= 0.05) return "hidden";
    // Up the composed tree, as `_INSPECT_JS` walks: through slots and shadow hosts.
    var up = function (n) { return n.assignedSlot || n.parentElement || (n.getRootNode && n.getRootNode().host) || null; };
    // The nearest scroll box below any fixed ancestor (the window's own scrolling,
    // html and body, is the scrollX/scrollY test below), and whether the field is
    // fixed with no scroll box between: then no scroll moves it.
    var doc = el.ownerDocument, fixed = cs.position === "fixed", sbox = null;
    for (var p = up(el); p; p = up(p)) {
      var ps = view.getComputedStyle(p);
      if (ps.display === "none" || ps.visibility === "hidden" || parseFloat(ps.opacity || "1") <= 0.05) return "hidden";
      if (fixed) continue;
      if (!sbox && p !== doc.body && p !== doc.documentElement
          && /auto|scroll/.test(ps.overflowX + " " + ps.overflowY)) sbox = p;
      if (ps.position === "fixed" && !sbox) fixed = true;
    }
    var cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    if (sbox) {
      var b = sbox.getBoundingClientRect();
      if (cx < b.left || cx >= b.right || cy < b.top || cy >= b.bottom) return "";
    }
    if (!(cx >= 0 && cy >= 0 && cx < view.innerWidth && cy < view.innerHeight)) {
      // Shopify parks a password-manager decoy in a fixed box past the window edge.
      return !fixed && cx + view.scrollX >= 0 && cy + view.scrollY >= 0 ? "" : "hidden";
    }
    var root = el.getRootNode ? el.getRootNode() : el.ownerDocument;
    var hit = (root && root.elementFromPoint ? root : el.ownerDocument).elementFromPoint(cx, cy);
    // No answer at all is not proof of an overlay: keep the ref.
    if (!hit || hit === el || el.contains(hit)) return "";
    return ownLabel(el, r, root, hit) ? "" : "covered";
  }

  // quick_ask.py `_INSPECT_JS` `ownLabel`: a control-free <label> inside the
  // field's own box that is one of its labels or sits in its small wrapper.
  function ownLabel(el, r, root, h) {
    var lab = h.closest ? h.closest("label") : null;
    if (!lab || lab.contains(el)) return false;
    if (lab.querySelector("input,select,textarea,button,a[href],iframe")) return false;
    var lr = lab.getBoundingClientRect();
    if (lr.left < r.left - 8 || lr.top < r.top - 8 || lr.right > r.right + 8 || lr.bottom > r.bottom + 8) return false;
    if (el.labels) for (var i = 0; i < el.labels.length; i++) if (el.labels[i] === lab) return true;
    if (lab.htmlFor) {
      var named = root && root.getElementById ? root.getElementById(lab.htmlFor) : null;
      if (named && named !== el) return false;
    }
    var box = el.parentElement;
    for (var up = 0; box && up < 2; up++, box = box.parentElement) {
      if (!box.contains(lab)) continue;
      var b = box.getBoundingClientRect();
      if (b.width > r.width * 2 + 48 || b.height > r.height * 3 + 48) return false;
      return box.querySelectorAll("input,select,textarea,button,a[href],iframe").length === 1;
    }
    return false;
  }

  // A box that failed visible() only for its size or offsetParent -- a 0 x 0
  // custom element host such as Reddit's auth-flow-manager, or an inline wrapper
  // -- still draws its children when nothing about it hides or clips them: the
  // snapshot walks through it like `display: contents`, emitting nothing for it.
  function drawsChildren(el, cs) {
    if (!cs || isFrame(el) || cs.display === "none") return false;
    if (cs.visibility === "hidden" || cs.visibility === "collapse" || cs.contentVisibility === "hidden") return false;
    var m = /opacity\(\s*([0-9.]+)(%?)\s*\)/.exec(cs.filter || "");
    if (parseFloat(cs.opacity || "1") * (m ? parseFloat(m[1]) / (m[2] ? 100 : 1) : 1) <= 0.05) return false;
    if (/hidden|clip|auto|scroll/.test(cs.overflow + " " + cs.overflowX + " " + cs.overflowY)) return false;
    if (/paint|strict|content/.test(cs.contain || "")) return false;
    if ((cs.clipPath || "none") !== "none" || (cs.clip || "auto") !== "auto") return false;
    return (cs.maskImage || cs.webkitMaskImage || "none") === "none";
  }

  function explicitRole(el) {
    var r = el.getAttribute && el.getAttribute("role");
    return r ? r.trim().toLowerCase() : "";
  }

  function inputType(el) {
    return ((el.tagName || "").toLowerCase() === "input")
      ? String(el.getAttribute("type") || "text").toLowerCase()
      : "";
  }

  function roleFor(el) {
    var role = explicitRole(el);
    if (role) return role;
    var tag = el.tagName.toLowerCase();
    if (tag === "a") return el.hasAttribute("href") ? "link" : "";
    if (tag === "button") return "button";
    if (tag === "select") return "combobox";
    if (tag === "textarea") return "textbox";
    if (tag === "input") {
      var t = inputType(el);
      if (t === "checkbox") return "checkbox";
      if (t === "radio") return "radio";
      if (t === "button" || t === "submit" || t === "reset") return "button";
      if (t === "hidden") return "";
      return "textbox";
    }
    if (/^h[1-6]$/.test(tag)) return "heading";
    return "";
  }

  function interactive(el, role) {
    var tag = el.tagName.toLowerCase();
    if (tag === "a") return el.hasAttribute("href");
    if (INTERACTIVE_TAGS[tag]) {
      if (tag === "input" && inputType(el) === "hidden") return false;
      return true;
    }
    if (el.isContentEditable) return true;
    var ti = el.getAttribute && el.getAttribute("tabindex");
    if (ti !== null && ti !== undefined && ti !== "-1" && ti !== "") return true;
    if (role && INTERACTIVE_ROLES[role]) return true;
    return false;
  }

  // A field the person filled through a quick ask (quick_ask.py marks it before the
  // write): the agent never reads, focuses, retypes or copies it.
  function asked(el) {
    return !!(el && el.hasAttribute && el.hasAttribute("data-kc-asked"));
  }

  // While the page holds a filled secret field (a quick-ask field, or a password or
  // secret-named one the person typed into during a takeover), an agent selector
  // may not test a value attribute (React mirrors the live value into it) in any
  // way: `body:has(...)`, a sibling combinator, a region or a `count` would answer
  // yes/no about the value one probe at a time. Escapes could spell "value", so a
  // backslash is refused too.
  function holdsSecretValue() {
    if (document.querySelector("[data-kc-asked]")) return true;
    var fields = document.querySelectorAll("input, textarea");
    for (var i = 0; i < fields.length; i++) {
      var el = fields[i];
      if (secretish(el) && (!!el.value || !!el.getAttribute("value"))) return true;
    }
    return false;
  }

  function probeSelectorBlocked(sel) {
    if (!holdsSecretValue()) return false;
    var s = String(sel).toLowerCase();
    return s.indexOf("value") !== -1 || s.indexOf("\\") !== -1;
  }

  // A field whose VALUE must never reach the model: the type says so, or the
  // name/id/autocomplete does. The snapshot drops its value and `type` refuses it.
  function secretish(el) {
    if (inputType(el) === "password") return true;
    // A field the person filled through a quick ask (quick_ask.py marks it before the
    // write): whatever it holds now is theirs, whatever its type or name says.
    if (asked(el)) return true;
    var auto = String(el.getAttribute("autocomplete") || "").toLowerCase().split(/\s+/);
    for (var i = 0; i < SECRET_AUTOCOMPLETE.length; i++) {
      if (auto.indexOf(SECRET_AUTOCOMPLETE[i]) !== -1) return true;
    }
    var named = (el.getAttribute("name") || "") + " " + (el.getAttribute("id") || "") + " " +
      (el.getAttribute("autocomplete") || "");
    // Both spellings: `apiKey` matches as written, `otpCode` only once split.
    return SECRET_NAME_RE.test(named) || SECRET_NAME_RE.test(splitCamel(named));
  }

  // A same-origin frame's document, or null (a cross-origin frame, or not a frame).
  // The snapshot walks into the first; the gateway reads the second through its
  // own CDP session (`browser_harness/frames.py`).
  function frameDoc(el) {
    var tag = (el && el.tagName || "").toLowerCase();
    if (tag !== "iframe" && tag !== "frame") return null;
    try { return el.contentDocument || null; } catch (e) { return null; }
  }

  function isFrame(el) {
    var tag = (el && el.tagName || "").toLowerCase();
    return tag === "iframe" || tag === "frame";
  }

  // From an element's own document to this one's viewport: the content boxes of
  // the same-origin frames it sits in, so a ref inside one is clicked where it is.
  function docOffset(el) {
    var x = 0, y = 0, doc = el.ownerDocument, guard = 0;
    while (doc && doc !== document && guard++ < 8) {
      var fe = null;
      try { fe = doc.defaultView && doc.defaultView.frameElement; } catch (e) { fe = null; }
      if (!fe) break;
      var b = fe.getBoundingClientRect();
      var cs = fe.ownerDocument.defaultView.getComputedStyle(fe);
      x += b.left + fe.clientLeft + (parseFloat(cs.paddingLeft) || 0);
      y += b.top + fe.clientTop + (parseFloat(cs.paddingTop) || 0);
      doc = fe.ownerDocument;
    }
    return [x, y];
  }

  function labelText(el) {
    var doc = el.ownerDocument || document;
    var lb = el.getAttribute && el.getAttribute("aria-labelledby");
    if (lb) {
      var parts = lb.split(/\s+/).map(function (id) {
        var n = doc.getElementById(id);
        return n ? (n.textContent || "").trim() : "";
      }).filter(Boolean);
      if (parts.length) return parts.join(" ");
    }
    var al = el.getAttribute && el.getAttribute("aria-label");
    if (al && al.trim()) return al.trim();
    if (el.id) {
      var sel = "label[for=\"" + (window.CSS && CSS.escape ? CSS.escape(el.id) : el.id) + "\"]";
      var lab = doc.querySelector(sel);
      if (lab && (lab.textContent || "").trim()) return lab.textContent.trim();
    }
    if (el.closest) {
      var wrap = el.closest("label");
      if (wrap && (wrap.textContent || "").trim()) return wrap.textContent.trim();
    }
    var secret = secretish(el);
    var attrs = secret ? ["alt", "title", "placeholder"] : ["alt", "title", "placeholder", "value"];
    for (var i = 0; i < attrs.length; i++) {
      var v = el.getAttribute && el.getAttribute(attrs[i]);
      if (v && v.trim()) return v.trim();
    }
    if (secret) return "";
    var own = (el.textContent || "").replace(/\s+/g, " ").trim();
    if (own || !el.querySelector || !el.querySelector("slot")) return own;
    return joinText(el.childNodes || [], 0).replace(/\s+/g, " ").trim();
  }

  function accName(el) {
    var n = labelText(el);
    if (n.length > NAME_CAP) n = n.slice(0, NAME_CAP) + "\u2026";
    return n;
  }

  function stateOf(el, role) {
    var st = {};
    if (el.disabled === true || (el.getAttribute && el.getAttribute("aria-disabled") === "true")) st.disabled = true;
    var ac = el.getAttribute && el.getAttribute("aria-checked");
    if (ac === "true") st.checked = true;
    else if ((role === "checkbox" || role === "radio") && el.checked === true) st.checked = true;
    var ae = el.getAttribute && el.getAttribute("aria-expanded");
    if (ae === "true") st.expanded = true;
    else if (ae === "false") st.expanded = false;
    if (inputType(el) === "password") st.password = true;
    if (inputType(el) === "file") st.file = true;
    return st;
  }

  function assignRef(el) {
    if (el.__kcRef && refs.get(el.__kcRef) === el) return el.__kcRef;
    var r = "e" + (++window.__kcRefSeq);
    try { Object.defineProperty(el, "__kcRef", { value: r, configurable: true }); }
    catch (e) { try { el.__kcRef = r; } catch (e2) {} }
    refs.set(r, el);
    return r;
  }

  function lookup(ref) {
    var el = refs.get(ref);
    return el && el.isConnected ? el : null;
  }

  // The element that really holds focus. `document.activeElement` stops at a
  // shadow HOST (a web-component login form), which is a <div> and never a
  // password input; the snapshot walks open shadow roots, so the fields inside
  // them get refs and must be judged by the same rule. Follow
  // `shadowRoot.activeElement` down as far as it goes (closed roots cannot be
  // entered and stop at the host, which the walker also never descends).
  //
  // Same-origin frames are followed the same way (their fields get refs too). A
  // cross-origin frame stops at its <iframe>: `activeField` says so, and the
  // gateway asks that frame itself.
  function deepActive() {
    var a = document.activeElement;
    var guard = 0;
    while (a && guard++ < 32) {
      if (a.shadowRoot && a.shadowRoot.activeElement) { a = a.shadowRoot.activeElement; continue; }
      var inner = frameDoc(a);
      if (inner && inner.activeElement && inner.activeElement !== inner.body) { a = inner.activeElement; continue; }
      break;
    }
    return a;
  }

  // Console capture: the Harness daemon drains CDP events into one shared
  // buffer, so console output is read in the page instead. The hook wraps the
  // console methods once per document and keeps the last CONSOLE_CAP entries.
  function installConsoleHook() {
    if (window.__kcConsoleDoc === document && Array.isArray(window.__kcConsole)) return;
    window.__kcConsole = [];
    window.__kcConsoleDoc = document;
    var buf = window.__kcConsole;
    function push(level, args) {
      var text = Array.prototype.map.call(args, function (a) {
        if (typeof a === "string") return a;
        try { return JSON.stringify(a); } catch (e) { return String(a); }
      }).join(" ");
      if (text.length > CONSOLE_TEXT_CAP) text = text.slice(0, CONSOLE_TEXT_CAP) + "\u2026";
      buf.push({ source: "console", level: level, text: text, ts: Date.now() });
      if (buf.length > CONSOLE_CAP) buf.splice(0, buf.length - CONSOLE_CAP);
    }
    ["log", "info", "warn", "error", "debug"].forEach(function (level) {
      var orig = console[level];
      if (typeof orig !== "function" || orig.__kcWrapped) return;
      var wrapped = function () { push(level, arguments); return orig.apply(console, arguments); };
      wrapped.__kcWrapped = true;
      try { console[level] = wrapped; } catch (e) {}
    });
    window.addEventListener("error", function (ev) {
      push("error", [String(ev && ev.message || "error")]);
    });
    window.addEventListener("unhandledrejection", function (ev) {
      push("error", ["Unhandled promise rejection: " + String(ev && ev.reason)]);
    });
  }

  // -- the reflex loop's observation (element table, change counters) ----------

  var TABLE_CAP = 250;
  var TABLE_TEXT_CAP = 3000;
  var READ_CAP = 4096;
  var TABLE_ROLES = {
    button: 1, link: 1, checkbox: 1, radio: 1, "switch": 1, tab: 1, menuitem: 1,
    menuitemradio: 1, option: 1, gridcell: 1, combobox: 1, textbox: 1, searchbox: 1,
    spinbutton: 1, slider: 1
  };
  var TABLE_SELECTOR = "a[href],button,input,textarea,select,summary,[contenteditable=\"true\"]," +
    Object.keys(TABLE_ROLES).map(function (r) { return "[role=\"" + r + "\"]"; }).join(",");
  var EDITABLE_ROLES = { textbox: 1, searchbox: 1, spinbutton: 1, combobox: 1 };

  // Change counters, once per document: DOM mutation records and animation frames.
  // A pipelined answer asked before the counter moved may be stale (`supersede`).
  function installCounters() {
    if (window.__kcTickDoc === document && window.__kcTick) return window.__kcTick;
    var tick = { m: 0, f: 0 };
    window.__kcTick = tick;
    window.__kcTickDoc = document;
    try {
      new MutationObserver(function (records) { tick.m += records.length; }).observe(
        document.documentElement, { subtree: true, childList: true, attributes: true, characterData: true }
      );
    } catch (e) {}
    (function frame() {
      tick.f++;
      try { requestAnimationFrame(frame); } catch (e) {}
    })();
    return tick;
  }

  function hash(text) {
    // FNV-1a, two seeds: a short fingerprint, never sent as page text.
    var a = 2166136261, b = 5381;
    for (var i = 0; i < text.length; i++) {
      var c = text.charCodeAt(i);
      a = Math.imul(a ^ c, 16777619) >>> 0;
      b = ((b << 5) + b + c) >>> 0;
    }
    return a.toString(16) + b.toString(16);
  }

  function shown(el) {
    if (el.closest && el.closest("[aria-hidden=\"true\"],[inert]")) return false;
    if (typeof el.checkVisibility === "function") {
      return el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true });
    }
    return visible(el);
  }

  function inViewport(r) {
    var x = r.left + r.width / 2, y = r.top + r.height / 2;
    return r.width > 0 && r.height > 0 && x >= 0 && y >= 0 && x < innerWidth && y < innerHeight;
  }

  function tableRole(el) {
    var role = explicitRole(el);
    if (TABLE_ROLES[role]) return role;
    var tag = el.tagName.toLowerCase();
    if (tag === "summary") return "button";
    if (el.isContentEditable && tag !== "input") return "textbox";
    if (tag === "input") {
      var t = inputType(el);
      if (t === "search") return "searchbox";
      if (t === "number") return "spinbutton";
      if (t === "range") return "slider";
      if (t === "image") return "button";
      if (["file", "hidden", "password", "color"].indexOf(t) !== -1) return "";
    }
    return roleFor(el);
  }

  function disabledEl(el) {
    return !!((el.matches && el.matches(":disabled")) || (el.closest && el.closest("[aria-disabled=\"true\"]")));
  }

  function editable(el, role) {
    if (el.readOnly || (el.getAttribute && el.getAttribute("aria-readonly") === "true")) return false;
    if (!EDITABLE_ROLES[role]) return false;
    var tag = el.tagName.toLowerCase();
    return role !== "combobox" || tag === "input" || tag === "textarea" || el.isContentEditable;
  }

  function fieldValue(el, role) {
    if (secretish(el)) return "";
    if ("value" in el && el.tagName.toLowerCase() !== "button") return String(el.value == null ? "" : el.value);
    if (el.isContentEditable || role === "combobox") return (el.innerText || "").trim().slice(0, NAME_CAP);
    var now = el.getAttribute && el.getAttribute("aria-valuenow");
    return now === null || now === undefined ? "" : String(now);
  }

  // The element's identity plus its meaning and its row/form/dialog context: an
  // answer about it is used only while this still matches.
  function guardOf(el, role) {
    var scope = (el.closest && el.closest("form,dialog,[role=\"dialog\"],article,li,tr,[role=\"row\"]")) || el.parentElement;
    return hash(JSON.stringify([
      el.__kcRef || "", role, accName(el), fieldValue(el, role), !!el.checked, el.selectedIndex == null ? -1 : el.selectedIndex,
      disabledEl(el), el.getAttribute("aria-expanded"), el.getAttribute("aria-checked"),
      el.getAttribute("aria-selected"), el.getAttribute("href"),
      scope && scope.innerText ? scope.innerText.slice(0, 2000) : ""
    ]));
  }

  function tableRow(el, role) {
    var row = { ref: assignRef(el), role: role, name: accName(el) || role, ops: [] };
    var value = fieldValue(el, role);
    if (value) row.value = value.slice(0, NAME_CAP);
    ["checked", "selected", "expanded"].forEach(function (key) {
      var v = el.getAttribute("aria-" + key);
      if (v !== null) row[key] = v;
    });
    if (role === "checkbox" || role === "radio") row.checked = String(!!el.checked);
    if (el.tagName.toLowerCase() === "select") {
      row.ops.push("select");
      row.options = [];
      for (var i = 0; i < el.options.length && row.options.length < 50; i++) {
        var o = el.options[i];
        if (o.selected || o.disabled || (o.closest && o.closest("optgroup[disabled]"))) continue;
        row.options.push({ i: i, label: (o.label || o.textContent || "").trim().slice(0, NAME_CAP), value: o.value });
      }
    } else {
      row.ops.push("click", "hover");
      if (editable(el, role)) row.ops.push("type");
    }
    row.g = guardOf(el, role);
    return row;
  }

  function regionRoot(selector) {
    if (!selector) return document.body || document.documentElement;
    if (probeSelectorBlocked(selector)) return null;
    try { return document.querySelector(String(selector)); } catch (e) { return null; }
  }

  function visibleText(root) {
    var words = [], length = 0, range = document.createRange();
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    var node;
    while ((node = walker.nextNode()) && length < TABLE_TEXT_CAP) {
      var value = (node.textContent || "").trim();
      var parent = node.parentElement;
      if (!value || !parent || parent.closest("script,style,noscript,template") || !shown(parent)) continue;
      if (parent.closest("input,textarea,select")) continue;
      range.selectNodeContents(node);
      var r = range.getBoundingClientRect();
      if (r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth) {
        words.push(value);
        length += value.length;
      }
    }
    return words.join("\n").slice(0, TABLE_TEXT_CAP);
  }

  // Declarative reads the agent names: never script. A field the outline would
  // withhold (password, secret-named) reads as null, and the whole answer is
  // capped at READ_CAP characters.
  function readProbe(probe) {
    var sel = probe && probe.sel, prop = String(probe && probe.prop || "text");
    var found;
    if (probeSelectorBlocked(sel)) return null;
    try { found = document.querySelectorAll(String(sel)); } catch (e) { return null; }
    if (prop === "count") return found.length;
    var el = found[0];
    if (!el) return null;
    // A secret field reads as null whatever is asked of it.
    if (inputType(el) === "password" || secretish(el)) return null;
    if (prop === "text") return (el.innerText || el.textContent || "").trim().slice(0, 200);
    if (prop === "value") return String(el.value == null ? "" : el.value).slice(0, 200);
    if (prop === "checked") return !!el.checked;
    if (prop === "box") {
      var b = el.getBoundingClientRect();
      return [Math.round(b.left), Math.round(b.top), Math.round(b.width), Math.round(b.height)];
    }
    if (prop.indexOf("attr:") === 0) {
      var v = el.getAttribute(prop.slice(5));
      return v === null ? null : String(v).slice(0, 200);
    }
    return null;
  }

  // The frames (`iframe`, `frame`, `object`, `embed`) one walk from `start`
  // (the top document by default; an element counts itself) reaches through open
  // shadow roots and readable frames: how many, and how many of them have a
  // document this world cannot read. Past depth 8 the walk stops and counts one
  // unreadable, so a guard refuses; `deep` counts those stops on their own, so
  // `read` can say text was left out for depth. `onRoot`, when given, is called
  // with each open shadow root and each readable iframe or frame document the
  // walk enters.
  function scanFrames(start, onRoot) {
    var n = 0;
    var seen = 0;
    var deep = 0;
    function embeds(el) {
      var tag = (el.tagName || "").toLowerCase();
      return tag === "iframe" || tag === "frame" || tag === "object" || tag === "embed";
    }
    function innerDoc(el) {
      try { return el.contentDocument || null; } catch (e) { return null; }
    }
    function walkRoot(root, d) {
      var all = Array.prototype.slice.call(root.querySelectorAll("*"));
      if (root.nodeType === 1) all.unshift(root);
      for (var j = 0; j < all.length; j++) {
        var el = all[j];
        var inner = embeds(el) ? innerDoc(el) : null;
        if (embeds(el)) seen++;
        // An `object` or `embed` counts even when its document is readable: the
        // secret-field counts only walk into iframe and frame documents.
        if (embeds(el) && (!inner || !isFrame(el))) n++;
        if (!el.shadowRoot && !inner) continue;
        // Too deep to look: count it, so the guard refuses rather than passes.
        if (d >= 8) { n++; deep++; return; }
        if (el.shadowRoot) {
          if (onRoot) onRoot(el.shadowRoot);
          walkRoot(el.shadowRoot, d + 1);
        }
        if (inner) {
          if (onRoot && isFrame(el)) onRoot(inner);
          walkRoot(inner, d + 1);
        }
      }
    }
    walkRoot(start || document, 0);
    return { unreadable: n, seen: seen, deep: deep };
  }

  // -- the `read` op: the page's content as text --------------------------------

  // The gateway pages the result; past this the page side stops, so a huge page
  // cannot make the worker's reply line unbounded.
  var READ_PAGE_CAP = 2000000;
  // The worker refuses a reply whose JSON passes 4 MiB (helper_worker
  // REPLY_CAP_BYTES), and JSON escapes each non-ASCII UTF-16 unit as 6 bytes, so
  // the content is cut by its escaped size, with headroom for the envelope.
  var READ_JSON_BUDGET = 3670016;
  // Form controls are left out like `innerText` leaves them out of the light DOM.
  var READ_SKIP = { script: 1, style: 1, template: 1, noscript: 1, textarea: 1, input: 1, select: 1 };

  // The longest prefix of `s` whose JSON-escaped size fits `budget` bytes.
  function capJsonSize(s, budget) {
    var used = 0;
    for (var i = 0; i < s.length; i++) {
      var c = s.charCodeAt(i);
      used += (c > 0x7e || c < 0x20) ? 6 : (c === 0x22 || c === 0x5c) ? 2 : 1;
      if (used > budget) {
        if (i > 0 && c >= 0xdc00 && c <= 0xdfff) i--;
        return s.slice(0, i);
      }
    }
    return s;
  }

  // Custom fields hold their value in child text, not an input's value property.
  function secretEditable(el) {
    if (!el || el.nodeType !== 1) return false;
    var ce = String(el.getAttribute("contenteditable")).toLowerCase();
    return (el.isContentEditable || ce === "" || ce === "true" || ce === "plaintext-only" ||
      !!EDITABLE_ROLES[explicitRole(el)]) && secretish(el);
  }

  // A scoped ref, shadow root or frame can start inside a protected field.
  function insideSecretEditable(node) {
    for (var n = node; n; ) {
      if (secretEditable(n)) return true;
      n = n.assignedSlot || n.parentNode || n.host ||
        (n.nodeType === 9 && n.defaultView && n.defaultView.frameElement) || null;
    }
    return false;
  }

  // A range can have rects even when its text is hidden. Match innerText's
  // exclusions across slots and shadow hosts, without requiring an element box.
  function textNodeShown(node) {
    var doc = node.ownerDocument || document;
    var range = doc.createRange ? doc.createRange() : null;
    if (!range || !range.getClientRects) return false;
    // Breaks and frames have no text contents; measure the element itself.
    if (node.localName === "br" || isFrame(node)) range.selectNode(node);
    else range.selectNodeContents(node);
    if (!range.getClientRects().length) return false;
    var view = doc.defaultView || window;
    var parentStyle = true, child = node;
    for (var n = node; n; ) {
      if (n.nodeType === 1) {
        var style = view.getComputedStyle(n);
        // Visibility inherits, but a descendant can explicitly restore it.
        if (parentStyle && style.visibility !== "visible") return false;
        parentStyle = false;
        if (style.display === "none" || style.contentVisibility === "hidden") return false;
        if (n.localName === "details" && !n.hasAttribute("open")) {
          var summary = n.firstElementChild;
          while (summary && summary.localName !== "summary") summary = summary.nextElementSibling;
          // Only this details' first summary stays rendered when it is closed.
          if (!summary || child !== summary) return false;
        }
      } else if (n.nodeType === 9) {
        try { n = n.defaultView && n.defaultView.frameElement; } catch (e) { return false; }
        if (!n) break;
        if (!n.getClientRects().length) return false;
        view = n.ownerDocument.defaultView || window;
        // Visibility does not inherit into a frame document.
        parentStyle = true;
        continue;
      }
      child = n;
      n = n.assignedSlot || n.parentNode || n.host || null;
    }
    return true;
  }

  // What select-all + copy gives for one node, the whole page and not only the
  // viewport. An element with a box gives its `innerText`, which leaves out
  // input values and what is not rendered. Any other element, and a shadow root,
  // has no `innerText` to trust (it falls back to `textContent`, script bodies
  // included), so its child nodes are read one by one. Each text node must have
  // a range box and pass the composed-tree checks that innerText would apply.
  function readBlock(el, style) {
    var display = style.display;
    if (display === "contents" || display === "none") return false;
    var parent = el.assignedSlot || el.parentElement || (el.parentNode && el.parentNode.host);
    var view = (el.ownerDocument || document).defaultView || window;
    var container = parent ? view.getComputedStyle(parent).display : "";
    // Flex/grid items form separate rendered runs even if their own display is inline.
    return container === "flex" || container === "inline-flex" ||
      container === "grid" || container === "inline-grid" ||
      ["block", "list-item", "table", "table-row", "table-caption", "flow-root", "flex", "grid"].indexOf(display) !== -1;
  }

  function renderedText(node) {
    var parts = [], space = false, breaks = 0;
    function append(text, preserve) {
      // Native innerText and preformatted text already carry meaningful spacing.
      if (preserve && text) {
        if (parts.length && breaks) parts.push("\n".repeat(breaks));
        else if (parts.length && space && !parts[parts.length - 1].endsWith("\n")) parts.push(" ");
        parts.push(text);
        space = false;
        breaks = 0;
        return;
      }
      for (var i = 0; i < text.length; i++) {
        var c = text[i];
        if (c === " " || c === "\t" || c === "\n" || c === "\r" || c === "\f") {
          space = true;
          continue;
        }
        if (parts.length && breaks) parts.push("\n".repeat(breaks));
        else if (parts.length && space && parts[parts.length - 1] !== "\n") parts.push(" ");
        space = false;
        breaks = 0;
        parts.push(c);
      }
    }
    function walk(node) {
      if (!node || insideSecretEditable(node)) return;
      var view = (node.ownerDocument || document).defaultView || window;
      var el = node.nodeType === 1 ? node :
        node.assignedSlot || node.parentElement || (node.parentNode && node.parentNode.host);
      var style = el ? view.getComputedStyle(el) : null;
      var preserve = style && ["pre", "pre-wrap", "pre-line", "break-spaces"].indexOf(style.whiteSpace) !== -1;
      if (node.nodeType === 3) {
        if (textNodeShown(node)) append(node.textContent || "", preserve);
        return;
      }
      var block = false;
      if (node.nodeType === 1) {
        if (READ_SKIP[(node.tagName || "").toLowerCase()]) return;
        if (node.localName === "br") {
          if (textNodeShown(node)) { breaks = Math.min(2, breaks + 1); space = false; }
          return;
        }
        block = readBlock(node, style);
        if (block) { breaks = Math.max(1, breaks); space = false; }
        // An ancestor's innerText includes custom field values. Split only those
        // ancestors, keeping the rendered text of every unaffected subtree.
        var descendants = Array.prototype.slice.call(node.querySelectorAll("*"));
        descendants.unshift(node);
        var hasSecret = descendants.some(function (el) {
          // Slotted light-DOM text can sit inside a protected shadow field.
          return secretEditable(el) || (el.shadowRoot &&
            Array.prototype.some.call(el.childNodes, insideSecretEditable));
        });
        if (!hasSecret && node.getClientRects && node.getClientRects().length > 0 &&
            typeof node.innerText === "string") {
          append(node.innerText, true);
          if (block) { breaks = Math.max(1, breaks); space = false; }
          else if (style.display === "table-cell") space = true;
          return;
        }
      } else if (node.nodeType !== 11) {
        return;
      }
      // Inline runs share whitespace state: splitting a word across spans must
      // not split a credential or a highlighted command into separate lines.
      for (var i = 0; i < node.childNodes.length; i++) walk(node.childNodes[i]);
      if (block) { breaks = Math.max(1, breaks); space = false; }
      else if (style && style.display === "table-cell") space = true;
    }
    walk(node);
    return parts.join("");
  }

  // The root's text, then the text of every open shadow root and same-origin
  // frame under it, which `innerText` does not enter. A frame this world cannot
  // read (cross-origin, or an `object` / `embed`) is counted, never read; so is
  // a root nested past the walk's depth, apart, in `deepRoots`.
  function readText(root) {
    var parts = [], size = 0;
    function add(node) {
      // Past the cap, not at it: one more root makes the content longer than the
      // cap, so `read` cuts it and says `truncated` instead of dropping it silently.
      if (size > READ_PAGE_CAP) return;
      if (node.nodeType === 9) {
        var frame;
        try { frame = node.defaultView && node.defaultView.frameElement; } catch (e) { return; }
        if (frame && (!frame.getClientRects().length || !textNodeShown(frame))) return;
      }
      var text = renderedText(node.nodeType === 9 ? node.body : node);
      // Do not trim indentation from a leading preformatted block.
      if (text.trim()) { parts.push(text); size += text.length; }
    }
    add(root);
    var scan = scanFrames(root, add);
    return { content: parts.join("\n\n"), skippedFrames: scan.unreadable - scan.deep, deepRoots: scan.deep };
  }

  var api = {
    // `cap`: the node budget left (the gateway walks cross-origin frames after this
    // document and hands each the rest). Same-origin frames are walked in place;
    // each cross-origin one is a `slot` node the gateway fills in.
    snapshot: function (cap) {
      var limit = (typeof cap === "number" && cap >= 0) ? Math.min(cap, NODE_CAP) : NODE_CAP;
      var slots = [];
      window.__kcFrameSlots = slots;
      var out = [];
      function emit(el, depth) {
        var role = roleFor(el);
        var name = accName(el);
        if (interactive(el, role)) {
          var st = stateOf(el, role);
          var why = st.password ? unaskable(el) : "";
          // A text box something lies over (a cookie or consent dialog) keeps its
          // ref -- typing and clicking still need it -- but says so: an ask on it
          // is refused "covered" until the agent closes what covers it.
          if (!st.password && /^(text|email|tel|search|url|number)$/.test(inputType(el))
              && unaskable(el) === "covered") {
            st.covered = true;
          }
          if (why) {
            // Listed with no ref, and why: "covered" is a dialog or banner the
            // agent can close; "hidden"/"disabled" a later step of the sign-in.
            st.unusable = why;
            out.push({ depth: depth, role: role || "generic", name: name, state: st });
            return depth + 1;
          }
          out.push({ depth: depth, role: role || "generic", name: name, ref: assignRef(el), state: st });
          return depth + 1;
        }
        if (role === "heading" && name) {
          out.push({ depth: depth, role: "heading", name: name });
          return depth + 1;
        }
        return depth;
      }
      var truncated = false;
      function walk(root, depth, frames) {
        var kids = root.children || [];
        for (var i = 0; i < kids.length; i++) {
          if (out.length >= limit) { truncated = true; return; }
          var el = kids[i];
          if (!el || el.nodeType !== 1) continue;
          var tag = el.tagName.toLowerCase();
          if (tag === "script" || tag === "style" || tag === "noscript" || tag === "template" || tag === "head") continue;
          // A `display: contents` wrapper draws no box of its own (0 x 0, no
          // offsetParent), but its children do: walk through it, emitting nothing
          // for it, and let each child judge its own visibility. A login form
          // wrapped in one (Adobe, ok.ru, Notion) is otherwise cut whole.
          var cs = window.getComputedStyle(el);
          if (cs && cs.display === "contents") {
            walk(el, depth, frames);
            if (el.shadowRoot) walk(el.shadowRoot, depth, frames);
            continue;
          }
          if (!visible(el)) {
            if (drawsChildren(el, cs)) {
              walk(el, depth, frames);
              if (el.shadowRoot) walk(el.shadowRoot, depth, frames);
            }
            continue;
          }
          if (isFrame(el)) {
            var title = (el.getAttribute("title") || el.getAttribute("name") || "").trim().slice(0, NAME_CAP);
            var inner = frameDoc(el);
            if (inner && inner.body && frames < 8) {
              out.push({ depth: depth, role: "iframe", name: title });
              walk(inner.body, depth + 1, frames + 1);
            } else if (!inner && slots.length < FRAME_SLOT_CAP) {
              out.push({ depth: depth, role: "iframe", name: title, slot: slots.length });
              slots.push(el);
            }
            continue;
          }
          var next = emit(el, depth);
          walk(el, next, frames);
          if (el.shadowRoot) walk(el.shadowRoot, next, frames);
        }
      }
      try {
        walk(document.body || document.documentElement, 0, 0);
      } catch (e) {
        return { url: location.href, title: document.title, nodes: out, error: String(e && e.message) };
      }
      var result = { url: location.href, title: document.title, nodes: out };
      if (truncated) result.error = "page has more than " + limit + " elements; the outline stops there";
      return result;
    },

    // The page's text for the `read` op, scoped to `ref` when one is given.
    // `skippedFrames` counts the frames left out, `deepRoots` the roots nested
    // too deep to read.
    read: function (ref) {
      var root = null;
      if (ref) {
        root = lookup(ref);
        if (!root) return { ok: false, code: "ref_stale" };
      }
      if (!root) root = document.body || document.documentElement;
      var read = readText(root);
      var out = { ok: true, url: String(location.href), title: String(document.title), skippedFrames: read.skippedFrames, deepRoots: read.deepRoots };
      var capped = read.content.length > READ_PAGE_CAP ? read.content.slice(0, READ_PAGE_CAP) : read.content;
      capped = capJsonSize(capped, READ_JSON_BUDGET);
      out.content = capped;
      if (capped.length < read.content.length) out.truncated = true;
      return out;
    },

    // Scroll a ref into view (unless `scroll` is false) and return its centre in CSS pixels.
    resolve: function (ref, scroll) {
      var el = lookup(ref);
      if (!el) return { ok: false, code: "ref_stale" };
      if (scroll !== false) {
        try { el.scrollIntoView({ block: "center", inline: "center" }); } catch (e) {}
      }
      var r = el.getBoundingClientRect();
      if (!r || (r.width === 0 && r.height === 0)) return { ok: false, code: "ref_stale" };
      var off = docOffset(el);
      return {
        ok: true,
        x: off[0] + r.left + r.width / 2,
        y: off[1] + r.top + r.height / 2,
        w: r.width,
        h: r.height,
        tag: (el.tagName || "").toLowerCase(),
        password: inputType(el) === "password"
      };
    },

    // Focus a ref and report whether focus actually landed and what it is.
    focus: function (ref) {
      var el = lookup(ref);
      if (!el) return { ok: false, code: "ref_stale" };
      if (inputType(el) === "password") return { ok: false, code: "password_field" };
      if (asked(el)) return { ok: false, code: "asked_field" };
      try { el.focus(); } catch (e) {}
      var active = deepActive();
      return {
        ok: true,
        focused: active === el,
        activeTag: (active && active.tagName || "").toLowerCase(),
        disabled: !!el.disabled,
        readOnly: !!el.readOnly
      };
    },

    // What holds focus now: the `type` op without a ref types into it.
    activeField: function () {
      var active = deepActive();
      var hasFocus = !!(document.hasFocus && document.hasFocus());
      if (!active || active === document.body) return { focused: false, password: false, asked: false, tag: "", hasFocus: hasFocus };
      if (isFrame(active)) {
        // Focus went into a frame: a same-origin one with nothing focused inside,
        // or a cross-origin one this page cannot see into (the gateway asks it).
        if (frameDoc(active)) return { focused: false, password: false, asked: false, tag: "", hasFocus: hasFocus };
        return { focused: true, frame: true, password: false, asked: false, tag: "iframe", hasFocus: hasFocus };
      }
      return {
        focused: true,
        password: inputType(active) === "password",
        asked: asked(active),
        // A filled field `secretish` names (an OTP, a token) that is not a password
        // box: a select or copy chord there would carry its value somewhere the
        // agent reads, as it would from an asked field.
        secret: inputType(active) !== "password" && secretish(active) && !!active.value,
        tag: (active.tagName || "").toLowerCase(),
        hasFocus: hasFocus
      };
    },

    // Paste into `ref`, or into what holds focus: `html` and `text` ride on a paste
    // event, which the page's editor reads as it would a clipboard paste, and the
    // clipboard itself is never written. A script's paste event inserts nothing on
    // its own, so `handled` (the page cancelled it, as an editor that takes the paste
    // does, and as a field that blocks pasting does too) tells the gateway whether
    // the plain text still has to be typed.
    paste: function (ref, html, text) {
      var el = ref ? lookup(ref) : deepActive();
      if (!el) return { ok: false, code: "ref_stale" };
      // A cross-origin frame's field is reached through its frame ref, never its <iframe>.
      if (isFrame(el)) return { ok: false, code: "focus_in_frame" };
      var data = new DataTransfer();
      if (html) data.setData("text/html", html);
      data.setData("text/plain", text);
      var event = new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true, composed: true });
      return { ok: true, handled: !el.dispatchEvent(event) };
    },

    selectOption: function (ref, values) {
      var el = lookup(ref);
      if (!el) return { ok: false, code: "ref_stale" };
      if ((el.tagName || "").toLowerCase() !== "select") return { ok: false, code: "not_select" };
      var set = {};
      for (var i = 0; i < values.length; i++) set[values[i]] = true;
      var selected = [];
      for (var j = 0; j < el.options.length; j++) {
        var opt = el.options[j];
        var match = !!(set[opt.value] || set[opt.label] || set[(opt.textContent || "").trim()]);
        opt.selected = match;
        if (match) selected.push(opt.value);
      }
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return { ok: true, selected: selected };
    },

    // The `upload` op's target: a visible, enabled <input type=file>. With `node`
    // the element itself, or null, for the helper to hand to DOM.setFileInputFiles;
    // without it the verdict, so the gateway can say why it refused.
    fileInput: function (ref, node) {
      var el = lookup(ref);
      var code = !el ? "ref_stale"
        : inputType(el) !== "file" ? "not_file_input"
        : disabledEl(el) ? "disabled"
        : !visible(el) ? "not_visible" : "";
      if (node) return code ? null : el;
      return code ? { ok: false, code: code } : { ok: true, multiple: !!el.multiple };
    },

    textPresent: function (text) {
      var b = document.body;
      return !!(b && (b.innerText || "").indexOf(text) !== -1);
    },

    // Navigation commit tracking: stamp the CURRENT document, then poll
    // `state` until the stamp is gone (a new document) or the href matches.
    stamp: function (token) {
      window.__kcNavToken = token;
      return true;
    },
    state: function () {
      return {
        readyState: String(document.readyState),
        stale: window.__kcNavToken || "",
        href: String(location.href),
        title: String(document.title)
      };
    },

    back: function () {
      if (window.history.length <= 1) return { ok: false, code: "no_history" };
      window.history.back();
      return { ok: true };
    },

    consoleMessages: function (limit) {
      installConsoleHook();
      var buf = window.__kcConsole || [];
      return buf.slice(Math.max(0, buf.length - limit));
    },

    // How many fields the person filled through a quick ask still hold a value in
    // the top document, open shadow roots included (an ask may bind a field in
    // one): the agent's `screenshot` is refused while this is not 0.
    askedValues: function () {
      var n = 0;
      function walkRoot(root, d) {
        var found = root.querySelectorAll("[data-kc-asked]");
        for (var i = 0; i < found.length; i++) if (found[i].value) n++;
        var all = root.querySelectorAll("*");
        for (var j = 0; j < all.length; j++) {
          var inner = frameDoc(all[j]);
          if (!all[j].shadowRoot && !inner) continue;
          // Too deep to look: count it, so the screenshot is refused, not let through.
          if (d >= 8) { n++; return; }
          if (all[j].shadowRoot) walkRoot(all[j].shadowRoot, d + 1);
          if (inner) walkRoot(inner, d + 1);
        }
      }
      walkRoot(document, 0);
      return n;
    },

    // How many filled fields `secretish` names hold a value the pixels would show:
    // an OTP, card code or token typed into a text field during a takeover, which
    // carries no quick-ask marker. A password input is left out (it renders as
    // dots) and so is `type=hidden` (it never renders). Open shadow roots and
    // same-origin frames are walked as `askedValues` walks them, and too deep to
    // look counts as one. The agent's `screenshot` is refused while this is not 0.
    secretValues: function () {
      var n = 0;
      function walkRoot(root, d) {
        var found = root.querySelectorAll("input, textarea");
        for (var i = 0; i < found.length; i++) {
          var el = found[i];
          var type = inputType(el);
          if (type === "password" || type === "hidden") continue;
          if (secretish(el) && !!el.value) n++;
        }
        var all = root.querySelectorAll("*");
        for (var j = 0; j < all.length; j++) {
          var inner = frameDoc(all[j]);
          if (!all[j].shadowRoot && !inner) continue;
          if (d >= 8) { n++; return; }
          if (all[j].shadowRoot) walkRoot(all[j].shadowRoot, d + 1);
          if (inner) walkRoot(inner, d + 1);
        }
      }
      walkRoot(document, 0);
      return n;
    },

    // How many frames in the top document (open shadow roots and same-origin
    // frames included) this world cannot read: a cross-origin frame has no
    // `contentDocument` here. With no frame inspector to count inside them, the
    // screenshot guards refuse while this is not 0.
    // Every such frame counts, hidden or not: a page can show a hidden frame for
    // the moment the image is taken and hide it again before either count, so
    // visibility is no proof it is absent from the pixels. `object` and `embed`
    // can load a page too, so they count like frames.
    unreadableFrames: function () {
      return scanFrames().unreadable;
    },

    // How many frames (and `object`/`embed`) this walk reached at all. A frame in
    // a closed shadow root is out of its reach; the gateway compares this with the
    // browser's own count of frame elements and refuses when the two differ.
    framesSeen: function () {
      return scanFrames().seen;
    },

    // How many password inputs have a box on the page, by the live view's mask
    // rule (screencast.py `_MASK_JS`): any `input[type=password]` with a non-empty
    // box, open shadow roots included to the same depth, plus the documents of
    // same-origin frames -- and any filled field `secretish` names (an OTP, a card
    // code, a token), whose value the outline also withholds. The decision hooks
    // send no screenshot while this is not 0.
    passwordBoxes: function () {
      var n = 0;
      function walkRoot(root, d) {
        var found = root.querySelectorAll("input, textarea");
        for (var i = 0; i < found.length; i++) {
          var el = found[i];
          var guarded = inputType(el) === "password" || (secretish(el) && !!el.value);
          if (!guarded) continue;
          var b = el.getBoundingClientRect();
          if (b.width > 0 && b.height > 0) n++;
        }
        if (d >= 8) return;
        var all = root.querySelectorAll("*");
        for (var j = 0; j < all.length; j++) {
          if (all[j].shadowRoot) walkRoot(all[j].shadowRoot, d + 1);
          var tag = (all[j].tagName || "").toLowerCase();
          if (tag === "iframe" || tag === "frame") {
            var inner = null;
            try { inner = all[j].contentDocument; } catch (e) { inner = null; }
            if (inner) walkRoot(inner, d + 1);
          }
        }
      }
      walkRoot(document, 0);
      return n;
    },

    // Whether an agent selector is refused because it could probe a filled secret
    // (`probeSelectorBlocked`): the gateway asks before a Harness helper runs one.
    selectorBlocked: function (selector) {
      return probeSelectorBlocked(selector);
    },

    // Visible elements matching a CSS selector, as {t, x, y} (short text and the
    // box centre in CSS pixels): the `reflex` op's game/grid state. Read-only;
    // form fields are skipped so a typed value is never read back. A selector the
    // page rejects answers {ok:false}.
    cells: function (selector) {
      var found;
      if (probeSelectorBlocked(selector)) return { ok: false, code: "bad_selector" };
      try { found = document.querySelectorAll(String(selector)); } catch (e) { return { ok: false, code: "bad_selector" }; }
      var out = [];
      for (var i = 0; i < found.length && out.length < 400; i++) {
        var el = found[i];
        var tag = (el.tagName || "").toLowerCase();
        if (tag === "input" || tag === "textarea" || tag === "select") continue;
        if (!visible(el)) continue;
        var b = el.getBoundingClientRect();
        out.push({ t: (el.textContent || "").trim().slice(0, 40), x: b.left + b.width / 2, y: b.top + b.height / 2 });
      }
      return { ok: true, cells: out };
    },

    // Boxes of visible <canvas> elements in the viewport, CSS pixels
    // [left, top, width, height]: the only targets the screenshot locator may
    // click, because the outline cannot see inside them.
    canvasRects: function () {
      var out = [];
      var rects = { w: window.innerWidth, h: window.innerHeight, rects: out };
      var all = document.querySelectorAll("canvas");
      for (var i = 0; i < all.length && out.length < 32; i++) {
        var el = all[i];
        if (!visible(el)) continue;
        var b = el.getBoundingClientRect();
        if (b.width < 16 || b.height < 16) continue;
        if (b.right <= 0 || b.bottom <= 0 || b.left >= window.innerWidth || b.top >= window.innerHeight) continue;
        out.push([b.left, b.top, b.width, b.height]);
      }
      return rects;
    },

    // ONE atomic read for one `reflex` decision. `opts`: `region` (a CSS selector
    // the table and text are limited to), `table` (the indexed element table),
    // `text`, `read` (declarative probes), `until` (text to look for). Always
    // carries the change counters and a fingerprint of what it read.
    observe: function (opts) {
      opts = opts || {};
      var tick = installCounters();
      var root = regionRoot(opts.region);
      var out = {
        ok: !!root, url: String(location.href), title: String(document.title),
        w: innerWidth, h: innerHeight,
        scroll: { y: Math.round(scrollY), height: document.documentElement.scrollHeight },
        mut: tick.m, frames: tick.f
      };
      if (!root) return out;
      var semantic = [];
      if (opts.table) {
        var rows = [], omitted = 0, found = root.querySelectorAll(TABLE_SELECTOR);
        for (var i = 0; i < found.length; i++) {
          var el = found[i], role = tableRole(el);
          if (!role || disabledEl(el) || !shown(el)) continue;
          if (role === "gridcell" && el.querySelector("button,[role=\"button\"]")) continue;
          if (!inViewport(el.getBoundingClientRect())) continue;
          if (rows.length >= TABLE_CAP) { omitted++; continue; }
          var row = tableRow(el, role);
          rows.push(row);
          semantic.push([row.ref, row.role, row.name, row.value || "", row.checked || "", row.expanded || ""]);
        }
        out.elements = rows;
        if (omitted) out.omitted = omitted;
      }
      if (opts.text) {
        out.text = visibleText(root);
        semantic.push(out.text);
      }
      if (Array.isArray(opts.read)) {
        var read = {}, size = 0;
        for (var j = 0; j < opts.read.length && j < 16; j++) {
          var value = readProbe(opts.read[j]);
          size += JSON.stringify(value === undefined ? null : value).length;
          if (size > READ_CAP) { read.truncated = true; break; }
          read[String(opts.read[j].name || ("r" + j))] = value;
        }
        out.read = read;
        semantic.push(read);
      }
      if (opts.until) {
        out.until = (document.body && (document.body.innerText || "").indexOf(String(opts.until)) !== -1) || false;
      }
      out.fp = hash(JSON.stringify([out.url, out.scroll.y, semantic]));
      return out;
    },

    // The executor's guard, just before input: the observed element still is what
    // the answer was about (same guard), is enabled, visible, in the viewport and
    // not covered at its centre; a typed-into field is editable and not secret; a
    // select still offers the option. Answers its centre, or why not.
    check: function (ref, kind, guard, optionIndex) {
      var el = lookup(ref);
      if (!el) return { ok: false, code: "gone" };
      var role = tableRole(el);
      if (guard && guardOf(el, role) !== guard) return { ok: false, code: "stale" };
      if (disabledEl(el) || !shown(el)) return { ok: false, code: "disabled" };
      if (kind === "type" && (secretish(el) || inputType(el) === "password")) return { ok: false, code: "password_field" };
      if (kind === "type" && !editable(el, role)) return { ok: false, code: "readonly" };
      var r = el.getBoundingClientRect();
      if (!inViewport(r)) return { ok: false, code: "offscreen" };
      var x = r.left + r.width / 2, y = r.top + r.height / 2;
      var hit = document.elementFromPoint(x, y);
      if (!hit || !(el === hit || el.contains(hit))) return { ok: false, code: "covered" };
      if (kind === "select") {
        var opt = el.options && el.options[optionIndex];
        if (!opt || opt.disabled) return { ok: false, code: "stale" };
        return { ok: true, value: opt.value };
      }
      return { ok: true, x: x, y: y };
    },

    // Focus a field and select its whole text, so the next typed text replaces it.
    selectText: function (ref) {
      var el = lookup(ref);
      if (!el) return { ok: false, code: "gone" };
      if (inputType(el) === "password" || secretish(el)) return { ok: false, code: "password_field" };
      try { el.focus(); } catch (e) {}
      try {
        if (typeof el.select === "function") el.select();
        else if (el.isContentEditable) {
          var range = document.createRange();
          range.selectNodeContents(el);
          var sel = window.getSelection();
          sel.removeAllRanges();
          sel.addRange(range);
        }
      } catch (e) {}
      return { ok: deepActive() === el };
    },

    // The change counters alone, for the short settle after an action.
    ticks: function () {
      var tick = installCounters();
      return { m: tick.m, f: tick.f };
    },

    // Whether a combobox's suggestions are on screen (its aria-controls/owns lists,
    // else any visible role=option): the settle after typing into one waits for this.
    optionsShown: function (ref) {
      var el = lookup(ref);
      var ids = el ? ((el.getAttribute("aria-controls") || el.getAttribute("aria-owns") || "").split(/\s+/).filter(Boolean)) : [];
      var roots = ids.length ? ids.map(function (id) { return document.getElementById(id); }).filter(Boolean) : [document];
      for (var i = 0; i < roots.length; i++) {
        var options = roots[i].querySelectorAll("[role=\"option\"]");
        for (var j = 0; j < options.length; j++) {
          if (shown(options[j]) && inViewport(options[j].getBoundingClientRect())) return true;
        }
      }
      return false;
    },

    // A cheap read of what an action can change, taken before and after it
    // (`browser.report_changes`): the address and title, the in-page dialogs, the
    // alert and status text, the name of each field the page marks invalid (never
    // its message), and a `role|name` key for each visible control. It mints no
    // refs and does not enter frames or shadow roots. A field is named by its
    // label, never by its value; a secret field, and anything inside a field, is
    // left out whole, and a name built from several elements is read whole or not
    // at all. `capped` names each list that stopped before the page
    // did, so the gateway never reads a cut list as a complete one.
    glance: function () {
      var out = { url: location.href, title: document.title, controls: [], dialogs: [],
                  alerts: [], invalid: [], truncated: false,
                  capped: { dialogs: false, alerts: false, invalid: false } };
      // Elements that are not text at all.
      var NOT_TEXT = { script: 1, style: 1, template: 1, noscript: 1, datalist: 1 };
      // Roles whose element shows its value as its own text, as any token of `role`.
      var FIELD_ROLES = ["textbox", "searchbox", "combobox", "spinbutton", "slider"];
      function fieldRole(n) {
        var tokens = spaceWords(explicitRole(n));
        for (var i = 0; i < tokens.length; i++) if (FIELD_ROLES.indexOf(tokens[i]) !== -1) return true;
        return false;
      }
      // Elements looked through in shadow roots, for the whole glance: a page past
      // it has its remaining shadow hosts counted as fields. Each host is looked
      // through once.
      var shadowBudget = GLANCE_SCAN_CAP * 4, hosts = new Map();
      // One line of *value*, whole, or null when it is longer than *cap*: a cut could
      // split a credential below the length the gateway's redaction matches.
      function clip(value, cap) {
        var text = squashSpace(String(value || ""));
        return text.length <= cap ? text : null;
      }
      // The one test of "this is a field", for the controls scan and for every text
      // read below: a native field, an editable region, an element with an input
      // role, or a shadow host whose open root holds one. What a field holds is a
      // value whatever its name says, so nothing inside one is ever read.
      function fieldish(n, depth) {
        var tag = n.tagName.toLowerCase();
        if (tag === "input") return ["button", "submit", "reset", "image"].indexOf(inputType(n)) === -1;
        if (tag === "textarea" || tag === "select" || n.isContentEditable) return true;
        if (fieldRole(n)) return true;
        if (!n.shadowRoot) return false;
        if (hosts.has(n)) return hosts.get(n);
        var holds = depth > 3;
        var all = holds ? [] : n.shadowRoot.querySelectorAll("*");
        shadowBudget -= all.length;
        if (shadowBudget < 0) holds = true;
        for (var i = 0; !holds && i < all.length; i++) holds = fieldish(all[i], depth + 1);
        hosts.set(n, holds);
        return holds;
      }
      function skipped(n) {
        return !!NOT_TEXT[n.tagName.toLowerCase()] || fieldish(n, 0);
      }
      // Whether *n*, or an element it sits in, is a field or not text: its text and
      // its attributes are then part of that field's value, and no name is read
      // from it. Checked before any name is read.
      function inField(n) {
        for (var up = n; up; up = up.parentElement) if (skipped(up)) return true;
        return false;
      }
      // Whether the page starts a new line at element *n*, whose computed display is
      // *display*: a line break, or a block box in normal flow. Inline boxes (and an
      // element with no computed display), and flex and grid items, which sit side
      // by side, carry on the line.
      function newLine(n, display) {
        if (n.tagName.toLowerCase() === "br") return true;
        if (!display || display.indexOf("inline") === 0 || display.indexOf("ruby") === 0 || display === "contents") return false;
        var around = n.parentElement ? getComputedStyle(n.parentElement).display : "";
        return around.indexOf("flex") === -1 && around.indexOf("grid") === -1;
      }
      // Whether the page draws the text of an element whose computed style is
      // *style*: `visibility` is inherited, so a `visible` child of a hidden
      // element is drawn and its hidden parent's own text is not.
      function drawn(style) {
        return style.visibility !== "hidden" && style.visibility !== "collapse";
      }
      // The text *node* shows, leaving out every field inside it, in one pass. A
      // node inside a field (an id an attribute points at, an alert in an editor)
      // is part of that field's value and reads as nothing, and text the page does
      // not draw (`display: none`, `visibility: hidden` or `collapse`) reads as
      // nothing and starts no new line. Text is joined as the page shows it: with
      // the white space the page holds, and a space where a new line starts, so a
      // credential split across inline elements reaches redaction whole. Text
      // longer than *cap* reads as null, as in `clip`; the raw text looked at,
      // spaces included, is bounded at four times *cap*.
      function ownText(node, cap) {
        if (inField(node)) return "";
        // `inks` holds, for each entry of `stack`, whether its parent is drawn.
        var text = "", length = 0, gap = false, LINE_END = {}, stack = [], inks = [];
        var ink = drawn(getComputedStyle(node));
        for (var c = node.lastChild; c; c = c.previousSibling) { stack.push(c); inks.push(ink); }
        while (stack.length) {
          var n = stack.pop();
          ink = inks.pop();
          if (n === LINE_END) { gap = true; continue; }
          if (n.nodeType === 3) {
            if (!ink) continue;
            length += n.nodeValue.length;
            if (length > cap * 4) return null;
            text += (gap ? " " : "") + n.nodeValue;
            gap = false;
            continue;
          }
          if (n.nodeType !== 1 || skipped(n) || n.hidden) continue;
          var style = getComputedStyle(n);
          if (style.display === "none") continue;
          ink = drawn(style);
          if (ink && newLine(n, style.display)) { gap = true; stack.push(LINE_END); inks.push(true); }
          for (c = n.lastChild; c; c = c.previousSibling) { stack.push(c); inks.push(ink); }
        }
        return clip(text, cap);
      }
      // The text of the elements *el*'s *attr* lists by id, read whole or not at all:
      // null when the list names more ids than are read, or when a listed element
      // is inside a field or reads as too long, so a name is never assembled from
      // part of its pieces. An id with no element adds nothing, as on the page.
      function idsText(el, attr) {
        var parts = [];
        var ids = spaceWords(el.getAttribute(attr) || "");
        if (ids.length > GLANCE_ID_CAP) return null;
        for (var i = 0; i < ids.length; i++) {
          var n = document.getElementById(ids[i]);
          if (!n) continue;
          var part = inField(n) ? null : ownText(n, GLANCE_TEXT_CAP);
          if (part === null) return null;
          parts.push(part);
        }
        return clip(parts.join(" "), GLANCE_TEXT_CAP);
      }
      // A control's name from its labels and attributes; a field's own text and
      // value never count, and a button's or link's own text does.
      function nameOf(el, isField) {
        var named = idsText(el, "aria-labelledby") || clip(el.getAttribute("aria-label"), GLANCE_NAME_CAP);
        if (named) return named;
        if (el.labels && el.labels.length) {
          var l = ownText(el.labels[0], GLANCE_NAME_CAP);
          if (l) return l;
        }
        if (!isField) {
          var own = ownText(el, GLANCE_NAME_CAP) || clip(el.getAttribute("alt") || el.getAttribute("value"), GLANCE_NAME_CAP);
          if (own) return own;
        }
        return clip(el.getAttribute("placeholder") || el.getAttribute("title") ||
                    el.getAttribute("name"), GLANCE_NAME_CAP);
      }
      var scanned = document.querySelectorAll(
        "a[href],button,input,select,textarea,[role],[tabindex],[contenteditable]");
      for (var i = 0; i < scanned.length; i++) {
        var el = scanned[i];
        if (i >= GLANCE_SCAN_CAP) { out.truncated = true; break; }
        var role = roleFor(el);
        if (!interactive(el, role) || !visible(el)) continue;
        // An element inside a field is part of its value: nothing of it is read.
        if (inField(el.parentElement)) continue;
        // A field is named by its labels only: its own text would be its value.
        var isField = fieldish(el, 0);
        if (isField && secretish(el)) continue;
        out.controls.push((role || "generic") + "|" + (clip(nameOf(el, isField), GLANCE_NAME_CAP) || ""));
      }
      var found = document.querySelectorAll("dialog[open],[role=\"dialog\"],[role=\"alertdialog\"]");
      for (var d = 0; d < found.length; d++) {
        if (!shown(found[d]) || inField(found[d].parentElement)) continue;
        if (out.dialogs.length >= GLANCE_LIST_CAP) { out.capped.dialogs = true; break; }
        var head = found[d].querySelector("h1,h2,h3,[role=\"heading\"]");
        out.dialogs.push(idsText(found[d], "aria-labelledby") ||
          clip(found[d].getAttribute("aria-label"), GLANCE_NAME_CAP) ||
          (head ? ownText(head, GLANCE_NAME_CAP) : "") || "dialog");
      }
      var live = document.querySelectorAll("[role=\"alert\"],[role=\"status\"],[aria-live=\"assertive\"]");
      for (var a = 0; a < live.length; a++) {
        if (!shown(live[a])) continue;
        var said = ownText(live[a], GLANCE_TEXT_CAP);
        // A message too long to read whole is still a message: it is said to be there.
        if (said === null) said = "a message too long to show";
        else if (!said) continue;
        if (out.alerts.length >= GLANCE_LIST_CAP) { out.capped.alerts = true; break; }
        out.alerts.push(said);
      }
      // Native fields, and any element marked invalid that the one field test names
      // a field (a custom `role="textbox"`, an editable region).
      var fields = document.querySelectorAll("input,select,textarea,[aria-invalid=\"true\"]");
      for (var f = 0; f < fields.length; f++) {
        if (f >= GLANCE_SCAN_CAP) { out.capped.invalid = true; break; }
        var field = fields[f];
        var flagged = field.getAttribute("aria-invalid") === "true";
        if (!flagged) {
          try { flagged = field.matches(":user-invalid"); } catch (e) { flagged = false; }
        }
        // A secret field, and a field inside another, is left out whole here too, as
        // in the controls above.
        if (!flagged || !fieldish(field, 0) || !shown(field) || secretish(field) ||
            inField(field.parentElement)) continue;
        if (out.invalid.length >= GLANCE_LIST_CAP) { out.capped.invalid = true; break; }
        // Named only: a validation message can repeat what was typed in any form,
        // so none is read. The agent takes a snapshot when it needs the message.
        out.invalid.push(clip(nameOf(field, true), GLANCE_NAME_CAP) || "field");
      }
      return out;
    },

    // The owner's media buttons on an app's page (`browser_harness/media.py`): play,
    // pause, seek or set the volume of the tab's main <video>/<audio>, and report its
    // state. Top document only. The element is the one playing, else the largest
    // shown one. Runs in the isolated world, so a page script cannot swap the
    // methods it calls. Reads no page text: only the element's own playback state.
    media: function (action, value) {
      var els = document.querySelectorAll("video,audio");
      var pick = null;
      var best = -1;
      for (var i = 0; i < els.length; i++) {
        var box = els[i].getBoundingClientRect();
        var score = (els[i].paused ? 0 : 1e12) + box.width * box.height;
        if (score > best) { best = score; pick = els[i]; }
      }
      if (!pick) return { ok: false, code: "no_media" };
      var num = typeof value === "number" && isFinite(value) ? value : null;
      if (action === "play") {
        try {
          var started = pick.play();
          if (started && typeof started.catch === "function") started.catch(function () {});
        } catch (e) { /* refused autoplay: the state below says paused */ }
      } else if (action === "pause") {
        pick.pause();
      } else if (action === "seek") {
        if (num === null || num < 0) return { ok: false, code: "bad_value" };
        pick.currentTime = num;
      } else if (action === "volume") {
        if (num === null || num < 0 || num > 1) return { ok: false, code: "bad_value" };
        pick.volume = num;
        if (num > 0) pick.muted = false;
      } else if (action !== "state") {
        return { ok: false, code: "bad_action" };
      }
      var r = pick.getBoundingClientRect();
      return {
        ok: true,
        kind: pick.tagName.toLowerCase(),
        paused: !!pick.paused,
        ended: !!pick.ended,
        currentTime: isFinite(pick.currentTime) ? pick.currentTime : 0,
        duration: isFinite(pick.duration) ? pick.duration : null,
        volume: pick.volume,
        muted: !!pick.muted,
        center: r.width > 0 && r.height > 0 ? [r.left + r.width / 2, r.top + r.height / 2] : null
      };
    }
  };
  return api;
})()
