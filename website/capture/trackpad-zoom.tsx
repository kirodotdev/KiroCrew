/**
 * Isolated capture entry for trackpad / ctrl+wheel magnification (PR #6262).
 *
 * WHY ISOLATED, and what it buys that the unit suite cannot: the unit specs in
 * `src/test/usePinchZoom.trackpad.test.tsx` fabricate their events, because jsdom
 * has no `GestureEvent` and its `WheelEvent` constructor drops every field it
 * inherits from MouseEvent. That pins the *arithmetic* and nothing about the
 * browser contract the feature actually rests on. Three claims are only decidable
 * in a real engine, and all three are the reason this entry exists:
 *
 *   1. A real Blink `wheel` carrying `ctrlKey` reaches a `window` listener
 *      registered NON-PASSIVELY, and `preventDefault()` on it is honoured.
 *      React attaches `wheel` passively at the root, so had this been an
 *      `onWheel` prop, Blink would emit
 *      "Unable to preventDefault inside passive event listener invocation"
 *      and the page would zoom anyway. The driver asserts that console error is
 *      ABSENT — which is what makes the manual `addEventListener` a measured
 *      decision rather than a stylistic one.
 *   2. The focal anchoring lands against REAL layout — real `offsetWidth`,
 *      real clamping — rather than the stubbed box a unit test has to inject.
 *   3. A PLAIN wheel is still delivered to the scroller, so a no-viewBox diagram
 *      keeps the scroll that is its only way to reach its edges.
 *
 * WHAT IS REAL: the real `DiagramLightbox`, the real `usePinchZoom`, production
 * classes, a real desktop viewport, real Blink event dispatch.
 * WHAT IS STOOD IN: nothing. The component takes a serialized SVG string, which
 * is exactly what `MarkdownRenderer` hands it from `mermaid.render`.
 */
import { createRoot } from 'react-dom/client'
import { useState } from 'react'
import DiagramLightbox from '../src/components/DiagramLightbox'
import { initI18n } from '../src/i18n'
// Production stylesheet. Without it the viewer renders unstyled and, worse, the
// `overflow-auto` scroller and flex centring do not exist — so the host's real
// `offsetWidth/Height`, which the pan clamp measures, would not be production's.
import '../src/index.css'

/** Mermaid-shaped output, built through the DOM: the component takes a
 *  SERIALIZED SVG string, so a serialized element is what this is. A viewBox is
 *  what makes the content fit-scaled, which is what enables the zoom path. */
function svgFixture(attrs: Record<string, string>): string {
  const NS = 'http://www.w3.org/2000/svg'
  const el = document.createElementNS(NS, 'svg')
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v)
  for (const [i, label] of ['start', 'middle', 'end'].entries()) {
    const t = document.createElementNS(NS, 'text')
    t.setAttribute('x', '4')
    t.setAttribute('y', String(20 + i * 30))
    t.setAttribute('font-size', '9')
    // Mermaid's output carries its own fills; a fixture without one renders
    // near-invisible on the dark overlay and would make a frame useless as
    // evidence while telling us nothing true about the component.
    t.setAttribute('fill', '#e8e8ea')
    t.textContent = label
    el.appendChild(t)
  }
  return el.outerHTML
}

const FITTED = svgFixture({ viewBox: '0 0 120 100' })
/** No viewBox: cannot fit-scale, so it keeps natural size and the scroller — not
 *  a transform — is what reaches its edges. The gesture must NOT be claimed here. */
const NATURAL = svgFixture({ width: '2400', height: '1800' })

function Harness() {
  // `?variant=natural` selects the no-viewBox case so one entry covers both
  // branches without the driver needing a second page.
  const natural = new URLSearchParams(window.location.search).get('variant') === 'natural'
  const [open, setOpen] = useState(true)
  if (!open) return <div data-testid="closed">closed</div>
  return <DiagramLightbox svg={natural ? NATURAL : FITTED} onClose={() => setOpen(false)} />
}

initI18n('en')
// Same theme convention as the sibling entries, so a frame matches production
// paint rather than the unstyled default.
const theme = new URLSearchParams(window.location.search).get('theme')
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
createRoot(document.getElementById('root')!).render(<Harness />)
