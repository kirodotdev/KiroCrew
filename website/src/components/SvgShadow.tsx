import { useLayoutEffect, useRef } from 'react'
import DOMPurify, { type Config } from 'dompurify'
import { guideUntrusted, stripGuideMarkers } from '../guide/untrustedContent'
import { svgOwnDeclarations, svgOwnStylesheet, svgOwnValue } from './svgStylesheet'

// Attributes SVG never reads as CSS: links, ids, classes, labels, data. `attr()`,
// the one way CSS could read them, is refused in every declaration. Every other
// attribute, a presentation attribute such as `fill` or one not named here, has
// its value checked as CSS.
const NOT_CSS = new Set(['id', 'class', 'href', 'xlink:href', 'role', 'tabindex', 'lang', 'xml:lang', 'xml:space', 'xmlns', 'xmlns:xlink'])
function isNotCss(name: string): boolean {
  return NOT_CSS.has(name) || name.startsWith('aria-') || name.startsWith('data-')
}
// SVG markup only: HTML in an SVG artifact (a `popover`, say) would draw in the
// top layer, over the page and past the host's containment.
const DEFAULT_PURIFY: Config = { USE_PROFILES: { svg: true, svgFilters: true } }

/**
 * An agent-authored SVG, sanitized and drawn in a shadow root of its own, so its
 * `<style>` elements style this drawing and nothing else: drawn straight into the
 * page, an SVG's stylesheet applies to the whole document, print included. Each
 * stylesheet keeps only the rules that select inside the drawing, and every
 * declaration and attribute keeps only drawing functions (svgStylesheet.ts), so
 * none of them can style the host or read the page around it.
 *
 * The host is the caller's element's only child, which the print preview hides
 * along with that element; nothing in the shadow root can restyle either. `css`
 * styles the `<svg>` from inside the root, where the page's classes cannot reach;
 * it is the caller's own text, so it is not filtered.
 */
export function SvgShadow({ content, purify = DEFAULT_PURIFY, css = '' }: { content: string; purify?: Config; css?: string }) {
  const hostRef = useRef<HTMLDivElement>(null)
  useLayoutEffect(() => {
    const host = hostRef.current
    if (!host) return
    const svg = DOMPurify.sanitize(content, { ...purify, RETURN_DOM_FRAGMENT: true })
    stripGuideMarkers(svg)
    for (const style of svg.querySelectorAll('style')) style.textContent = svgOwnStylesheet(style.textContent ?? '')
    for (const el of svg.querySelectorAll('*')) {
      for (const { name, value } of Array.from(el.attributes)) {
        if (name === 'style') {
          const kept = svgOwnDeclarations(value)
          if (kept) el.setAttribute(name, kept)
          else el.removeAttribute(name)
        } else if (!isNotCss(name) && !svgOwnValue(value)) {
          el.removeAttribute(name)
        }
      }
    }
    const own = document.createElement('style')
    own.textContent = css
    ;(host.shadowRoot ?? host.attachShadow({ mode: 'open' })).replaceChildren(own, svg)
  }, [content, purify, css])
  // Layout and paint containment hold the drawing inside the host's box: a
  // `position: fixed` or transformed `<svg>` cannot lay itself over the page. The
  // rest lays the `<svg>` out as it was as a direct flex item of the caller's box:
  // the host stretches to that box, so a percent height still resolves against
  // it, and scrolls a drawing larger than it, as the caller's box did.
  return <div ref={hostRef} className="flex items-center justify-center self-stretch w-full min-w-0 min-h-0 overflow-auto contain-layout contain-paint" {...guideUntrusted} />
}
