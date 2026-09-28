/**
 * Evidence for the sketch-pad cursor-offset bug.
 *
 * THE PROBLEM: Excalidraw captures its container's viewport offsets ONCE in
 * `componentDidMount` (`updateDOMRect` -> `getBoundingClientRect`) and refreshes
 * them only on window resize, debounced scroll, or a ResizeObserver on its own
 * container. A CSS `transform` animation fires none of those — the layout box
 * never changes — so a pad mounted while `DialogContent`'s `zoom-in-95` /
 * `slide-in-from-top-[48%]` enter animation is still running keeps the
 * MID-ANIMATION offsets for the rest of the dialog's life. Every pointer event
 * is then mapped through the wrong origin, which is what makes a click land
 * beside the cursor and a resize handle miss.
 *
 * The scene mounts the REAL `SketchDialog` — real Radix `DialogContent`, real
 * Excalidraw, real `index.css` animations — and adds NOTHING to the product
 * component: the probe reads Excalidraw's own dev-build test hook
 * (`window.h.state`, defined in its `componentDidMount`) and the container's
 * live rect, so the driver MEASURES the offset instead of assuming it.
 *
 *   window.__warm()  — resolves the lazy Excalidraw chunk without opening, so a
 *                      later open mounts synchronously the way every open after
 *                      the first one does in production.
 *   window.__open()  / window.__close()
 *   window.__probe() — { believed, actual, delta }. `delta` is the pointer
 *                      mapping error in CSS pixels: a stale offset of d makes
 *                      every shape land d pixels away from the cursor.
 */
import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { initI18n } from '../src/i18n/all'
import { LanguageProvider } from '../src/i18n/LanguageProvider'
import SketchDialog from '../src/components/SketchDialog'
import '../src/index.css'

document.documentElement.setAttribute('data-theme', 'kiro-dark')
document.documentElement.dataset.mode = 'dark'
initI18n('en')

type Probe = {
  believed: { offsetLeft: number; offsetTop: number; width: number; height: number }
  actual: { left: number; top: number; width: number; height: number }
  delta: { left: number; top: number; width: number; height: number }
} | null

declare global {
  interface Window {
    /** Excalidraw's own dev/test hook. */
    h?: { state?: { offsetLeft: number; offsetTop: number; width: number; height: number } }
    __warm: () => Promise<void>
    __open: () => void
    __close: () => void
    __probe: () => Probe
  }
}

const round = (n: number) => Math.round(n * 100) / 100

window.__warm = async () => {
  await import('@excalidraw/excalidraw')
}

window.__probe = () => {
  const s = window.h?.state
  const container = document.querySelector('.excalidraw')
  if (!s || !(container instanceof HTMLElement)) return null
  const r = container.getBoundingClientRect()
  return {
    believed: {
      offsetLeft: round(s.offsetLeft),
      offsetTop: round(s.offsetTop),
      width: round(s.width),
      height: round(s.height),
    },
    actual: { left: round(r.left), top: round(r.top), width: round(r.width), height: round(r.height) },
    delta: {
      left: round(s.offsetLeft - r.left),
      top: round(s.offsetTop - r.top),
      width: round(s.width - r.width),
      height: round(s.height - r.height),
    },
  }
}

function Scene() {
  const [open, setOpen] = useState(false)
  window.__open = () => setOpen(true)
  window.__close = () => setOpen(false)
  return (
    <div data-capture-root style={{ width: '100%', height: '100%', background: 'var(--bg)', color: 'var(--text)' }}>
      <button onClick={() => setOpen(true)}>open sketch</button>
      <SketchDialog open={open} onOpenChange={setOpen} onInsert={() => {}} />
    </div>
  )
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={qc}>
    <LanguageProvider>
      <Scene />
    </LanguageProvider>
  </QueryClientProvider>,
)
