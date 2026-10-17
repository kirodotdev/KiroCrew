/**
 * Isolated capture entry for the image viewer's swipe-down-to-dismiss gesture.
 *
 * WHY ISOLATED: the behaviour under test is real input injection against real
 * layout. The gesture only exists for a COARSE pointer, tracks the finger with a
 * live transform, and its threshold is measured against the viewport — none of
 * which happy-dom can observe (it computes no layout) and none of which a still
 * unit test can show as motion. The live dashboard cannot stand in either: it is
 * token-gated, and reaching a chat message with an image is many steps of setup
 * the gesture does not depend on.
 *
 * The faithful part is the COMPONENT: the real `Lightbox` is imported and opened
 * through the same `lightbox` CustomEvent `dispatchLightbox()` fires in
 * production, so the overlay being dragged is exactly the shipped one.
 *
 * The subject image is an inline SVG data URI rather than a file, so the capture
 * has no network dependency and renders identically on every machine.
 *
 * window.__swipe() reports the live gesture state — the wrapper's transform and
 * the backdrop's computed background — so a capture run can assert the image
 * actually moved instead of only producing a frame that looks plausible.
 */
import { useEffect } from 'react'
import { createRoot } from 'react-dom/client'
import { initI18n } from '../src/i18n'
import { Lightbox } from '../src/components/MarkdownRenderer'
import '../src/index.css'

const params = new URLSearchParams(location.search)
// Light by default ON PURPOSE: the overlay is `bg-black/80` in every theme, so a
// bright page behind it is what makes the dim — and its fade during a drag —
// legible in a still. A dark page behind a dark scrim shows nothing.
const theme = params.get('theme') || 'light'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

/** A stand-in "screenshot" with enough structure that a shrink or a translate is
 *  visible at a glance, and enough contrast to read against the dimmed backdrop. */
const SUBJECT = `data:image/svg+xml;utf8,${encodeURIComponent(`
<svg xmlns="http://www.w3.org/2000/svg" width="900" height="1400" viewBox="0 0 900 1400">
  <rect width="900" height="1400" fill="#0e1116"/>
  <rect x="0" y="0" width="900" height="96" fill="#1b212b"/>
  <circle cx="56" cy="48" r="18" fill="#7c5cff"/>
  <rect x="92" y="38" width="220" height="20" rx="10" fill="#38404d"/>
  <rect x="48" y="152" width="804" height="220" rx="18" fill="#151a22"/>
  <rect x="80" y="188" width="420" height="22" rx="11" fill="#3d4756"/>
  <rect x="80" y="228" width="640" height="16" rx="8" fill="#2b333f"/>
  <rect x="80" y="258" width="560" height="16" rx="8" fill="#2b333f"/>
  <rect x="80" y="288" width="600" height="16" rx="8" fill="#2b333f"/>
  <rect x="48" y="416" width="804" height="420" rx="18" fill="#151a22"/>
  <rect x="80" y="452" width="300" height="22" rx="11" fill="#3d4756"/>
  <rect x="80" y="500" width="740" height="280" rx="12" fill="#10151c"/>
  <rect x="112" y="540" width="380" height="14" rx="7" fill="#2b333f"/>
  <rect x="112" y="576" width="520" height="14" rx="7" fill="#2b333f"/>
  <rect x="112" y="612" width="300" height="14" rx="7" fill="#2b333f"/>
  <rect x="112" y="648" width="460" height="14" rx="7" fill="#2b333f"/>
  <rect x="48" y="880" width="804" height="220" rx="18" fill="#151a22"/>
  <rect x="80" y="916" width="500" height="22" rx="11" fill="#3d4756"/>
  <rect x="80" y="956" width="700" height="16" rx="8" fill="#2b333f"/>
  <rect x="80" y="986" width="620" height="16" rx="8" fill="#2b333f"/>
  <rect x="48" y="1144" width="804" height="200" rx="18" fill="#151a22"/>
  <rect x="80" y="1180" width="360" height="22" rx="11" fill="#3d4756"/>
  <rect x="80" y="1220" width="680" height="16" rx="8" fill="#2b333f"/>
  <rect x="80" y="1250" width="540" height="16" rx="8" fill="#2b333f"/>
</svg>`)}`

interface SwipeState {
  open: boolean
  transform: string
  backdrop: string
  /** The <img>'s own transform, which carries the pinch scale and the pan. Read
   *  separately from `transform` because the two live on different elements on
   *  purpose: the wrapper holds the dismiss offset so it composes with, rather
   *  than fights, the zoom. */
  image: string
}

declare global {
  interface Window {
    __swipe: () => SwipeState
  }
}

window.__swipe = () => {
  const overlay = document.querySelector<HTMLElement>('[role="button"].fixed.inset-0')
  if (!overlay) return { open: false, transform: 'none', backdrop: 'none', image: 'none' }
  const inner = overlay.firstElementChild as HTMLElement
  const img = overlay.querySelector('img')
  return {
    open: true,
    transform: getComputedStyle(inner).transform,
    backdrop: getComputedStyle(overlay).backgroundColor,
    image: img ? getComputedStyle(img).transform : 'none',
  }
}

function Scene() {
  // Open through the production event path rather than a prop, so the capture
  // exercises the same listener a real image click goes through.
  useEffect(() => {
    window.dispatchEvent(new CustomEvent('lightbox', {
      detail: { images: [{ src: SUBJECT, alt: 'A dashboard screenshot' }], index: 0 },
    }))
  }, [])
  return (
    <div className="bg-bg text-text min-h-[844px]">
      {/* Page content behind the overlay, so the backdrop's dim (and its fade
          during a drag) is visible rather than black-on-black. */}
      <div className="p-4 space-y-3">
        <div className="h-6 w-40 rounded bg-chrome" />
        <div className="h-4 w-full rounded bg-chrome" />
        <div className="h-4 w-5/6 rounded bg-chrome" />
        <div className="h-40 w-full rounded-lg bg-chrome" />
        <div className="h-4 w-2/3 rounded bg-chrome" />
        <div className="h-4 w-full rounded bg-chrome" />
        <div className="h-24 w-full rounded-lg bg-chrome" />
      </div>
      <Lightbox />
    </div>
  )
}

initI18n('en')
createRoot(document.getElementById('root')!).render(<Scene />)
