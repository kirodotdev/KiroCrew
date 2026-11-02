"use strict";

// Page-realm helpers the element-annotation overlay (browser-annotate.js)
// splices into the page: the ref map (`window.__kcRefs`, reset on a real
// navigation), `assignRef`, the accessible-name and role heuristics, and
// visibility. The `eN` an annotation writes into the chat draft is minted here.
//
// A FUNCTION BODY fragment: splice it into an IIFE, never evaluate it alone.
// No Electron import, no DOM access of its own; the module holds source text.

/** Accessible-name cap, in characters, before an ellipsis. */
const NAME_CAP = 120;

/** id of the element-annotation overlay's host node. A fixed identifier-shaped
 *  constant, so it is spliced into page-realm source as a plain quoted literal
 *  (no runtime serialisation of anything variable). */
const ANNOTATE_HOST_ID = "__kcAnnotateHost";

const PAGE_HELPERS_SOURCE = `
  var CAP = ${NAME_CAP};
  var INTERACTIVE_TAGS = { a: true, button: true, input: true, select: true, textarea: true };
  var INTERACTIVE_ROLES = { button:1, link:1, checkbox:1, radio:1, tab:1, menuitem:1, option:1, "switch":1, combobox:1, textbox:1 };

  // The ref map lives as long as the DOCUMENT object: a real navigation swaps
  // the document (and, normally, this whole JS realm), while an SPA route
  // change only rewrites the URL -- its elements, and any ref already handed
  // out for them (an annotation, an agent's last snapshot), stay valid.
  if (window.__kcRefDocObj !== document || !(window.__kcRefs instanceof Map)) {
    window.__kcRefs = new Map();
    window.__kcRefSeq = 0;
    window.__kcRefDocObj = document;
  }
  var refs = window.__kcRefs;
  // Refs are strong references. An SPA that re-renders subtrees across many
  // route changes would otherwise pin every element ever walked for the
  // document's lifetime; every consumer already rejects a disconnected ref
  // (resolveRef -> ref_stale, the overlay -> detached), so dropping those
  // entries here changes no answer and bounds the map to the live DOM.
  refs.forEach(function (el, r) { if (!el || !el.isConnected) refs.delete(r); });

  function visible(el) {
    var style = window.getComputedStyle(el);
    if (!style) return false;
    if (style.display === "none" || style.visibility === "hidden") return false;
    var rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return false;
    if (el.offsetParent === null && style.position !== "fixed") return false;
    return true;
  }

  function explicitRole(el) {
    var r = el.getAttribute && el.getAttribute("role");
    return r ? r.trim().toLowerCase() : "";
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
      var t = (el.getAttribute("type") || "text").toLowerCase();
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
      if (tag === "input" && (el.getAttribute("type") || "").toLowerCase() === "hidden") return false;
      return true;
    }
    if (el.isContentEditable) return true;
    var ti = el.getAttribute && el.getAttribute("tabindex");
    if (ti !== null && ti !== undefined && ti !== "-1" && ti !== "") return true;
    if (role && INTERACTIVE_ROLES[role]) return true;
    return false;
  }

  function labelText(el) {
    var lb = el.getAttribute && el.getAttribute("aria-labelledby");
    if (lb) {
      var parts = lb.split(/\\s+/).map(function (id) {
        var n = document.getElementById(id);
        return n ? (n.textContent || "").trim() : "";
      }).filter(Boolean);
      if (parts.length) return parts.join(" ");
    }
    var al = el.getAttribute && el.getAttribute("aria-label");
    if (al && al.trim()) return al.trim();
    if (el.id) {
      var sel = "label[for=\\"" + (window.CSS && CSS.escape ? CSS.escape(el.id) : el.id) + "\\"]";
      var lab = document.querySelector(sel);
      if (lab && (lab.textContent || "").trim()) return lab.textContent.trim();
    }
    if (el.closest) {
      var wrap = el.closest("label");
      if (wrap && (wrap.textContent || "").trim()) return wrap.textContent.trim();
    }
    var attrs = ["alt", "title", "placeholder", "value"];
    var tag = (el.tagName || "").toLowerCase();
    var itype = tag === "input" ? String(el.getAttribute("type") || "text").toLowerCase() : "";
    // Never read a secret out of the DOM into the snapshot: the outline goes
    // straight into the agent's context (and its transcript), so a populated
    // password/credential field would leak the value verbatim. Drop the value
    // attribute for those inputs and fall through to label/placeholder instead.
    var secretish =
      itype === "password" ||
      /(^|[^a-z])(password|passwd|pwd|otp|totp|mfa|cvv|cvc|secret|token|apikey)([^a-z]|$)/i.test(
        (el.getAttribute("name") || "") + " " + (el.getAttribute("id") || "") + " " +
          (el.getAttribute("autocomplete") || "")
      );
    if (secretish) {
      attrs = ["alt", "title", "placeholder"];
    }
    for (var i = 0; i < attrs.length; i++) {
      var v = el.getAttribute && el.getAttribute(attrs[i]);
      if (v && v.trim()) return v.trim();
    }
    if (secretish) return "";
    return (el.textContent || "").replace(/\\s+/g, " ").trim();
  }

  function accName(el) {
    var n = labelText(el);
    if (n.length > CAP) n = n.slice(0, CAP) + "\\u2026";
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


`;

module.exports = { ANNOTATE_HOST_ID, NAME_CAP, PAGE_HELPERS_SOURCE };
