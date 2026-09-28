/**
 * Isolated capture entry for "the shell does not page-zoom on touch".
 *
 * WHY ISOLATED, and why a capture at all: the claim is about a BROWSER decision,
 * not a React one. Nothing in the component tree can be asserted to prove it —
 * the evidence is `visualViewport.scale` still reading 1 after a genuine
 * two-finger spread, which needs real touch injection against real layout in a
 * mobile-emulated context. happy-dom computes no layout and has no visual
 * viewport, so the unit tests can only pin the DECLARATIONS (that the meta says
 * `user-scalable=no`, that the root `touch-action` is `pan-x pan-y`); whether the
 * engine then honours them is exactly what this page exists to measure.
 *
 * WHAT MAKES IT HONEST: the viewport meta is not authored here. It is read out of
 * the shipped `index.html` at build time and installed at runtime, so a capture
 * run tests the app's own string — a meta edited in index.html and forgotten here
 * cannot pass. `?zoomable=1` installs a permissive meta instead, giving the run a
 * CONTROL: if the control does not zoom, the harness cannot detect zoom at all
 * and a green subject run would prove nothing.
 *
 * The JS half of the suppression (`utils/pageZoom.ts`) is deliberately NOT
 * installed. It exists for WebKit's non-standard gesture events, which Chromium
 * never fires; installing it here would add a listener that cannot run and invite
 * the reading that Chromium's result depends on it. Its own behaviour is unit
 * tested, and the iOS half needs a real device.
 *
 * window.__zoom() reports the live visual-viewport scale and the resolved root
 * touch-action, so a run asserts measurements rather than producing a frame that
 * merely looks unzoomed.
 */
import { createRoot } from 'react-dom/client'
import indexHtml from '../index.html?raw'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const zoomable = params.get('zoomable') === '1'

document.documentElement.setAttribute('data-theme', 'kiro-light')

/** The shipped viewport meta, verbatim. Throwing rather than falling back is the
 *  point: a silent default would let this capture pass with a meta the app does
 *  not ship. */
function shippedViewport(): string {
  const m = indexHtml.match(/<meta name="viewport" content="([^"]*)"/)
  if (!m) throw new Error('no viewport meta found in index.html')
  return m[1]
}

const meta = document.createElement('meta')
meta.name = 'viewport'
// The control keeps `width=device-width` so layout is identical to the subject
// run — the ONLY difference between the two is whether zoom is permitted.
meta.content = zoomable ? 'width=device-width, initial-scale=1' : shippedViewport()
document.head.appendChild(meta)

// The control must relax the ROOT TOUCH-ACTION too, not just the meta. Both halves
// of the suppression are on this page (index.css is imported), so a control that
// only swapped the meta would still be pinch-proof — and would report "the harness
// cannot detect zoom" when what it actually proved is that the CSS rule works.
if (zoomable) document.documentElement.style.touchAction = 'auto'

interface ZoomState {
  scale: number
  touchAction: string
  viewport: string
}

declare global {
  interface Window {
    __zoom: () => ZoomState
  }
}

window.__zoom = () => ({
  // `visualViewport.scale` is the engine's own answer to "is this page zoomed",
  // which is the reading no DOM assertion can substitute for.
  scale: window.visualViewport?.scale ?? 1,
  touchAction: getComputedStyle(document.documentElement).touchAction,
  viewport: meta.content,
})

function Scene() {
  return (
    <div className="bg-bg text-text">
      {/* High-contrast NUMBERED rows, not the usual neutral placeholder bars: the
          frames these runs produce are compared against each other, and a
          low-contrast skeleton renders as a blank page at both scales — proving
          nothing to a human reading the evidence even when the assertions pass.
          Numbers make the scale legible at a glance: the control frame shows two
          or three rows, the subject frame shows all of them.

          Tall enough to scroll, because the rule must withhold pinch and
          double-tap WITHOUT taking scrolling with them — and a page that cannot
          scroll would hide a `touch-action: none` regression completely. */}
      <div className="p-4 space-y-3">
        <div className="text-2xl font-semibold">Page zoom capture</div>
        {Array.from({ length: 24 }, (_, i) => (
          <div
            key={i}
            className="flex h-12 items-center gap-3 rounded-lg px-3 text-lg font-semibold"
            style={{
              background: i % 2 === 0 ? '#7c5cff' : '#1b212b',
              color: i % 2 === 0 ? '#ffffff' : '#c9d1dc',
            }}
          >
            <span className="tabular-nums">{String(i + 1).padStart(2, '0')}</span>
            <span className="text-sm font-normal opacity-80">row {i + 1} of 24</span>
          </div>
        ))}
      </div>
    </div>
  )
}

createRoot(document.getElementById('root')!).render(<Scene />)
