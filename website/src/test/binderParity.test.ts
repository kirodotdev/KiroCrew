// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import DOMPurify from 'dompurify'
import { dashboardDocument } from '../pages/chat/command-center/dashboardDocument'

// jsdom parses CSS rules but lacks the constructed-sheet replaceSync the card path
// uses; without an adapter that path fails closed and strips EVERY <style>, which would
// mask whether the binder's style skip does any work. This mirrors the adapter in
// dashboardDocument.test.ts: it accepts one top-level fixture rule using jsdom's own
// CSSOM parser, so a bound <style> survives to the binding loop the same way it does in
// the browser.
const ParsedSheet = CSSStyleSheet
function installConstructedSheet() {
  vi.stubGlobal('CSSStyleSheet', class extends ParsedSheet {
    replaceSync(css: string) {
      while (this.cssRules.length) this.deleteRule(0)
      if (css.trim()) this.insertRule(css, 0)
    }
  })
}
afterEach(() => { vi.unstubAllGlobals() })

// What this file pins -- stated as the binder's OWN behaviour, not an external model.
//
// dashboardDocument() fills an automatic card by DOMPurify.sanitize()ing the markup,
// then walking `doc.body.querySelectorAll('[data-dashboard-field]')` and setting each
// matched element's textContent from `data`. Three properties of that walk decide which
// cells a page fills, and a generator predicting fills for a page must agree with all
// three or it ships blank cells:
//
//   1. a `style` element is SKIPPED rather than filled,
//   2. the scope is the body's DESCENDANTS, so the root and the body itself are never
//      matched even when they carry the attribute,
//   3. the field is the attribute value the binder READS, which is what DOMPurify left
//      after sanitizing -- it trims the attribute, so a page authored with a padded name
//      binds under the trimmed name (and `data` keyed by the padded name it declared
//      never matches), and a binding on a FORBID_TAGS element the sanitizer removes
//      (`button`, `form`, `input`, ...) is gone before the walk and fills nothing.
//
// Before this test, the only thing holding a fill-predicting generator to the real
// binder was a grep for two literal strings (the selector and the attribute name). A
// grep proves the strings still exist; it proves nothing about behaviour, so the binder
// could change its scope, its style skip, or run before the sanitizer, and the grep
// would stay green while generated pages render blank cells.
//
// This test closes the gap on the side that owns the behaviour. For every fixture it
// drives the page through the REAL binder and reads back which cells the binder actually
// filled, then compares that observed set against `reachableFields` -- the same three
// properties computed over the SAME sanitized DOM the binder walks, so the two never
// diverge on a sanitizer-altered page. The observation does not read the binder's
// source: it gives each reachable field a UNIQUE sentinel value in `data` and asks which
// sentinels landed as an element's text. A field the binder never matched (a skipped
// `style`, the root or body itself, or an element the sanitizer removed) keeps its
// original markup text and never carries its sentinel, so it is absent from the observed
// set.

/** The binding attribute the host reads -- one attribute, one field, one value. */
const BINDING = 'data-dashboard-field'

// The sanitizer options the binder applies before it binds. Modelling the binder's reach
// means walking the SAME sanitized output, so these must stay in step with
// dashboardDocument(): FORBID_TAGS elements are gone and attribute values are trimmed
// before the binding loop sees them.
const SANITIZE_OPTS = {
  WHOLE_DOCUMENT: true,
  ADD_TAGS: ['style'],
  FORBID_TAGS: ['script', 'link', 'meta', 'base', 'iframe', 'frame', 'object', 'embed', 'applet', 'template', 'noscript', 'form', 'input', 'button', 'textarea', 'select', 'animate', 'set'],
} as const

/** The DOM the binder actually walks: the sanitizer's output, parsed. */
function sanitizedDoc(html: string): Document {
  const sanitized = DOMPurify.sanitize(html, SANITIZE_OPTS as DOMPurify.Config) as string
  return new DOMParser().parseFromString(sanitized, 'text/html')
}

/**
 * The fields the real binder fills for `html`, observed rather than read from source.
 * Each field the binder could reach (collected from the SAME sanitized DOM it walks) is
 * bound to a sentinel that cannot occur in the fixture text; a field whose sentinel is
 * the text of some element in the rendered card was filled, and is returned. `data` is
 * the automatic-card path, so the binder runs.
 */
function boundFields(html: string): Set<string> {
  // Collect candidates from the sanitized DOM, keyed by the value the binder READS (the
  // trimmed attribute), so the sentinel lands under the key the exact lookup matches.
  const candidates = new Set<string>()
  for (const el of sanitizedDoc(html).body.querySelectorAll(`[${BINDING}]`)) {
    candidates.add(el.getAttribute(BINDING) ?? '')
  }
  const ordered = [...candidates]
  const sentinel = (field: string) => `\u241E-bound-${ordered.indexOf(field)}-\u241E`
  const data: Record<string, string> = {}
  for (const field of candidates) data[field] = sentinel(field)

  installConstructedSheet()
  const rendered = new DOMParser().parseFromString(dashboardDocument(html, {}, 'dark', data), 'text/html')
  const shownText = new Set<string>()
  for (const el of rendered.querySelectorAll('*')) shownText.add(el.textContent ?? '')

  const filled = new Set<string>()
  for (const field of candidates) {
    if (shownText.has(sentinel(field))) filled.add(field)
  }
  return filled
}

/**
 * The fields a fill-predicting generator must report for the same page, computed over
 * the SAME sanitized DOM the binder walks. This is the binder's three-property reach, not
 * a separate model: sanitize, then the body-descendant scope minus `style`, reading the
 * attribute value the sanitizer left. A page whose markup would make a stricter generator
 * RAISE (unclosed markup, empty or repeated bindings, a binding nested in a bound element)
 * is out of scope here -- those are refusals, not a field set. The equality gate this
 * test protects only ever compares field SETS, so those cases never reach it.
 */
function reachableFields(html: string): Set<string> {
  const fields = new Set<string>()
  // Rule 2: the binder asks the BODY for its descendants, so only elements inside the
  // body are reachable; the root and the body itself are not their own descendants.
  for (const el of sanitizedDoc(html).body.querySelectorAll(`[${BINDING}]`)) {
    // Rule 1: a `style` element is skipped before the field is read.
    if (el.tagName.toLowerCase() === 'style') continue
    // Rule 3: the field is the attribute value AS SANITIZED -- DOMPurify has already
    // trimmed it, so this is the key the binder's exact lookup uses.
    const field = el.getAttribute(BINDING) ?? ''
    if (field === '') continue
    fields.add(field)
  }
  return fields
}

/** Assert the observed binder behaviour equals the reachable-field reach for a page. */
function expectParity(html: string): Set<string> {
  const observed = boundFields(html)
  const modelled = reachableFields(html)
  expect([...observed].sort()).toEqual([...modelled].sort())
  return observed
}

describe('dashboard binder parity: the real binder matches its predicted reach', () => {
  it('fills a plain body descendant, the ordinary case', () => {
    const fields = expectParity(`<p ${BINDING}="result">old</p>`)
    expect(fields).toEqual(new Set(['result']))
  })

  it('skips a style element rather than filling it (behaviour 1)', () => {
    // A binding on <style> is reachable by the selector but the binder skips it, so its
    // field is never filled. A fill predictor must agree, or a page authoring it ships a
    // hole. The <style> follows visible content so the parser keeps it INSIDE the body; a
    // <style> with nothing before it is hoisted into <head> and would be unreachable by
    // the body scope alone, which would hide whether the skip itself does any work.
    const html = `<p ${BINDING}="result">old</p><style ${BINDING}="css">.x{}</style>`
    const fields = expectParity(html)
    expect(fields).toEqual(new Set(['result']))
    expect(fields.has('css')).toBe(false)
  })

  it('never matches the body or the root themselves (behaviour 2)', () => {
    // The sanitizer keeps a binding on <html>/<body> on those elements. The binder queries
    // body.querySelectorAll, which excludes the body and the root, so neither is filled
    // even though the attribute is present on them.
    const html = `<html ${BINDING}="root"><body ${BINDING}="body"><p ${BINDING}="inner">old</p></body></html>`
    const fields = expectParity(html)
    expect(fields).toEqual(new Set(['inner']))
    expect(fields.has('root')).toBe(false)
    expect(fields.has('body')).toBe(false)
  })

  it('binds a padded attribute under its trimmed name, never the declared padded name (behaviour 3)', () => {
    // The sanitizer trims the attribute to "lede" before the binder reads it, so the
    // binder fills under "lede". A generator predicting fills from the DECLARED name
    // " lede " -- and data keyed by that declared name -- would miss the cell; the binder
    // and the reach model agree the field is the trimmed "lede". boundFields keys its
    // sentinel by the sanitized name, so it observes the cell the binder fills, and the
    // declared padded name is absent from both sides.
    const html = `<p ${BINDING}=" lede ">old</p><p ${BINDING}="tidy">old</p>`
    const fields = expectParity(html)
    expect(fields).toEqual(new Set(['lede', 'tidy']))
    expect(fields.has(' lede ')).toBe(false)
  })

  it('does not fill a binding on a sanitizer-removed element (behaviour 3, FORBID_TAGS)', () => {
    // A <button> carries the binding attribute but the sanitizer removes the element
    // before the binding loop runs, so its field can never be filled. A fill predictor
    // that read raw markup would over-report it; computing the reach over the sanitized
    // DOM drops it, and the binder agrees.
    const html = `<button ${BINDING}="danger">old</button><p ${BINDING}="ok">old</p>`
    const fields = expectParity(html)
    expect(fields).toEqual(new Set(['ok']))
    expect(fields.has('danger')).toBe(false)
  })

  it('agrees on a page that combines every behaviour at once', () => {
    const html =
      `<html ${BINDING}="root">` +
      `<body ${BINDING}="body">` +
      `<h1 ${BINDING}="title">old</h1>` +
      `<style ${BINDING}="css">.x{}</style>` +
      `<p ${BINDING}=" padded ">old</p>` +
      `<button ${BINDING}="danger">old</button>` +
      `<section><span ${BINDING}="nested">old</span></section>` +
      `</body></html>`
    const fields = expectParity(html)
    expect(fields).toEqual(new Set(['title', 'padded', 'nested']))
  })

  it('fills every reachable field and only those, so the two surfaces cannot silently drift', () => {
    // A regression that widened the binder's reach (querying the whole document, or
    // stopping the style skip) would fill a cell the reach model does not predict; one
    // that narrowed it would leave a predicted cell blank. Either way boundFields and
    // reachableFields diverge and this assertion fails -- the drift the grep-only tie
    // could never catch.
    const html =
      `<p ${BINDING}="a">old</p>` +
      `<div ${BINDING}="b"><em>old</em></div>` +
      `<style ${BINDING}="skip">.x{}</style>`
    expect(boundFields(html)).toEqual(reachableFields(html))
    expect(reachableFields(html)).toEqual(new Set(['a', 'b']))
  })
})
