import { describe, it, expect, vi } from 'vitest'
import { createEvent, fireEvent, render, screen } from '@testing-library/react'
import { LocalOverlayRoot } from '../components/overlayRoot'
import DiagramLightbox from '../components/DiagramLightbox'

/** Built through the DOM, as in DiagramLightbox.zoom.test.tsx: the component
 *  takes a serialized SVG string. */
function svgFixture(): string {
  const el = document.createElementNS('http://www.w3.org/2000/svg', 'svg')
  el.setAttribute('width', '40')
  el.setAttribute('height', '30')
  return el.outerHTML
}

// A keyboard chord or a notification click can switch Crews while the viewer is
// open: the overlay root hides it, and its own keys must stand down.
describe('DiagramLightbox across Crew switches', () => {
  it('hides and ignores Escape while a remote Crew shows, then returns intact', () => {
    const onClose = vi.fn()
    const svg = svgFixture()
    const ui = (hidden: boolean) => (
      <LocalOverlayRoot hidden={hidden}><DiagramLightbox svg={svg} onClose={onClose} /></LocalOverlayRoot>
    )
    const view = render(ui(false))
    const dialog = screen.getByRole('dialog')

    view.rerender(ui(true))
    expect(dialog.closest('[data-local-overlay-root]')).toHaveStyle({ display: 'none' })
    const esc = createEvent.keyDown(window, { key: 'Escape' })
    fireEvent(window, esc)
    expect(onClose).not.toHaveBeenCalled()
    expect(esc.defaultPrevented).toBe(false)
    expect(screen.getByRole('dialog', { hidden: true })).toBe(dialog)

    view.rerender(ui(false))
    expect(screen.getByRole('dialog')).toBe(dialog)
    expect(dialog.closest('[data-local-overlay-root]')).not.toHaveStyle({ display: 'none' })
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})
