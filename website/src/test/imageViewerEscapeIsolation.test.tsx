/**
 * Image/diagram viewer — Escape isolation (#20630).
 *
 * Both full-viewport viewers (the image `Lightbox` and `DiagramLightbox`) open
 * ABOVE an artifact that is itself dismissed by Escape: `ArtifactPanel` /
 * `MarkdownPanel` bind `document.addEventListener('keydown', ...)` in the
 * BUBBLE phase and, unlike the `Modal` family, do NOT check `defaultPrevented`
 * — so `preventDefault()` alone does not spare them. Opening a diagram inside
 * an artifact and pressing Escape therefore closed BOTH the viewer and the
 * artifact underneath it.
 *
 * The fix is `stopPropagation()` in each viewer's capture-phase Escape handler:
 * the capture listener runs before the event descends to any bubble-phase host
 * listener, so stopping it there keeps the artifact's Escape handler from ever
 * firing, while the viewer still closes (the image viewer from its own handler,
 * the diagram viewer from `useDialogFocusTrap`'s capture listener).
 *
 * These specs stand in for the artifact with a plain bubble-phase keydown spy —
 * the exact binding ArtifactPanel/MarkdownPanel use — and assert it never hears
 * the Escape that closes the viewer, while a non-Escape key still reaches
 * capture-phase listeners so the fix stays surgical (it must not starve the
 * viewer's own window-capture keyboard).
 */
import { render, act, screen } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import DiagramLightbox from '../components/DiagramLightbox'
import { Lightbox } from '../components/MarkdownRenderer'

/** A serialized mermaid-shaped SVG with a viewBox (the fit-scaled case). Built
 *  through the DOM because the component inserts a serialized string via
 *  `createContextualFragment`, and because authored viewBox markup in a `.tsx`
 *  trips the use-lucide-icons rule (see DiagramLightbox.zoom.test.tsx). */
function svgWithViewBox(): string {
  const NS = 'http://www.w3.org/2000/svg'
  const el = document.createElementNS(NS, 'svg')
  el.setAttribute('viewBox', '0 0 100 100')
  const label = document.createElementNS(NS, 'text')
  label.textContent = 'n1'
  el.appendChild(label)
  return el.outerHTML
}

/** Dispatch a browser-faithful bubbling, cancelable Escape at `document` — the
 *  same path a real keypress takes, so a capture-phase listener on `window` (an
 *  ancestor) runs first, then bubble-phase document/window listeners. */
function pressEscape(): void {
  act(() => {
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
  })
}

describe('DiagramLightbox — Escape isolation (#20630)', () => {
  it('closes the viewer but does not reach a bubble-phase artifact Escape handler', () => {
    const onClose = vi.fn()
    const artifactEscape = vi.fn()
    document.addEventListener('keydown', artifactEscape)
    try {
      render(<DiagramLightbox svg={svgWithViewBox()} onClose={onClose} />)
      pressEscape()
      // The viewer closed (via useDialogFocusTrap's capture listener)…
      expect(onClose).toHaveBeenCalledTimes(1)
      // …and the artifact's bubble-phase handler never heard the Escape.
      expect(artifactEscape).not.toHaveBeenCalled()
    } finally {
      document.removeEventListener('keydown', artifactEscape)
    }
  })

  it('still lets a non-Escape key reach a capture-phase listener (fix stays surgical)', () => {
    const capturing = vi.fn()
    document.addEventListener('keydown', capturing, { capture: true })
    try {
      render(<DiagramLightbox svg={svgWithViewBox()} onClose={() => {}} />)
      act(() => {
        document.dispatchEvent(new KeyboardEvent('keydown', { key: 'a', bubbles: true, cancelable: true }))
      })
      expect(capturing).toHaveBeenCalled()
    } finally {
      document.removeEventListener('keydown', capturing, { capture: true })
    }
  })
})

describe('image Lightbox — Escape isolation (#20630)', () => {
  function openLightbox(): void {
    render(<Lightbox />)
    act(() => {
      window.dispatchEvent(new CustomEvent('lightbox', {
        detail: { images: [{ src: 'a.png', alt: 'a' }], index: 0 },
      }))
    })
    expect(screen.getByAltText('a')).toBeInTheDocument()
  }

  it('closes the viewer but does not reach a bubble-phase artifact Escape handler', () => {
    const artifactEscape = vi.fn()
    document.addEventListener('keydown', artifactEscape)
    try {
      openLightbox()
      pressEscape()
      // The image viewer closed itself…
      expect(screen.queryByAltText('a')).not.toBeInTheDocument()
      // …and the artifact's bubble-phase handler never heard the Escape.
      expect(artifactEscape).not.toHaveBeenCalled()
    } finally {
      document.removeEventListener('keydown', artifactEscape)
    }
  })

  it('still lets a non-Escape key reach a capture-phase listener (fix stays surgical)', () => {
    const capturing = vi.fn()
    document.addEventListener('keydown', capturing, { capture: true })
    try {
      openLightbox()
      act(() => {
        document.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true, cancelable: true }))
      })
      expect(capturing).toHaveBeenCalled()
    } finally {
      document.removeEventListener('keydown', capturing, { capture: true })
    }
  })
})
