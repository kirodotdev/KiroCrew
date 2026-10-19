import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { useRef } from 'react'
import { LocalOverlayRoot } from '../components/overlayRoot'
import SelectionToolbar, { useSelectionActions } from '../components/SelectionToolbar'

// The toolbar floats above the page rather than inside the local pane, so it is
// the overlay root, not the pane's display:none, that must hide it.
function Harness() {
  const ref = useRef<HTMLDivElement>(null)
  const actions = useSelectionActions(() => {})
  return (
    <div ref={ref}>
      <SelectionToolbar containerRef={ref} actions={actions} externalSelection={{ text: 'hi', x: 10, y: 10 }} />
    </div>
  )
}

describe('SelectionToolbar across Crew switches', () => {
  it('hides while a remote Crew is showing and returns intact with the local one', () => {
    const view = render(<LocalOverlayRoot hidden={false}><Harness /></LocalOverlayRoot>)
    // Asserted on the root's `display` rather than toBeVisible(): the entrance
    // fade starts at opacity 0 and framer-motion does not run it under jsdom.
    const quote = screen.getByRole('button', { name: 'Quote' })
    const root = () => quote.closest('[data-local-overlay-root]')
    expect(root()).not.toHaveStyle({ display: 'none' })

    view.rerender(<LocalOverlayRoot hidden><Harness /></LocalOverlayRoot>)
    expect(root()).toHaveStyle({ display: 'none' })

    view.rerender(<LocalOverlayRoot hidden={false}><Harness /></LocalOverlayRoot>)
    expect(screen.getByRole('button', { name: 'Quote' })).toBe(quote)
    expect(root()).not.toHaveStyle({ display: 'none' })
  })
})
