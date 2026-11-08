import { useLayoutEffect, useRef } from 'react'
import DOMPurify, { type Config } from 'dompurify'
import { guideUntrusted, stripGuideMarkers } from '../guide/untrustedContent'

const DEFAULT_PURIFY: Config = {}

/**
 * An agent-authored SVG, sanitized and drawn in a shadow root of its own, so its
 * `<style>` elements style this drawing and nothing else: drawn straight into the
 * page, an SVG's stylesheet applies to the whole document, print included.
 *
 * The host is `display: contents`, so the `<svg>` lays out as a child of the
 * caller's element, which must hold nothing else: the print preview hides that
 * element, and nothing in the shadow root can restyle it. `css` styles the
 * `<svg>` from inside the root, where the page's classes cannot reach.
 */
export function SvgShadow({ content, purify = DEFAULT_PURIFY, css = '' }: { content: string; purify?: Config; css?: string }) {
  const hostRef = useRef<HTMLDivElement>(null)
  useLayoutEffect(() => {
    const host = hostRef.current
    if (!host) return
    const svg = DOMPurify.sanitize(content, { ...purify, RETURN_DOM_FRAGMENT: true })
    stripGuideMarkers(svg)
    const own = document.createElement('style')
    own.textContent = css
    ;(host.shadowRoot ?? host.attachShadow({ mode: 'open' })).replaceChildren(own, svg)
  }, [content, purify, css])
  return <div ref={hostRef} className="contents" {...guideUntrusted} />
}
