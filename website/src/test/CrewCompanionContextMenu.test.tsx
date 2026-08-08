/**
 * Can the pet overlay's right-click menu be dismissed by clicking away?
 *
 * The overlay window is click-through except over the rects the renderer reports to
 * the main process. The menu used to report only its OWN box, so a click just
 * outside it was forwarded to the desktop and never reached this page — the
 * close-on-outside listener could never fire and the menu was stuck open ("I can no
 * longer click anywhere to dismiss it"). On top of that, `pet.html` sets
 * `pointer-events: none` on html AND body, so an empty-area click hits no DOM
 * element and dispatches no event at all.
 *
 * The fix, pinned here: while the menu is open in the overlay it reports the WHOLE
 * viewport as its interactive region (so the overlay accepts clicks everywhere), and
 * it renders a transparent full-viewport backdrop (a real element the outside click
 * can land on). The rect is cleared the instant the menu closes, so no stale
 * full-screen hitbox is left capturing the user's screen.
 *
 * These are DOM-level assertions — they bypass the OS hit-test entirely, so a pass
 * here means the renderer half is correct; the Electron half is pinned separately in
 * electron/crew-companion/test/petHitbox.test.js.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, fireEvent, cleanup } from '@testing-library/react'

import { ContextMenu, type ContextMenuEntry } from '../apps/crew-companion/ContextMenu'
import { petBridge } from '../apps/crew-companion/petBridge'

const items: ContextMenuEntry[] = [
  { label: 'Change avatar', action: 'gallery' },
  { separator: true },
  { label: 'Turn off companion', action: 'quit', danger: true },
]

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('overlay context menu — click-outside dismissal', () => {
  it('reports the WHOLE viewport as its hitbox while open, not just the menu box', () => {
    const setMenuHitbox = vi.spyOn(petBridge, 'setMenuHitbox').mockImplementation(() => {})
    render(
      <ContextMenu x={10} y={10} items={items} reportHitbox onAction={() => {}} onClose={() => {}} />,
    )
    // Full viewport, so the overlay captures a click anywhere while the menu is open.
    // Reporting only the small menu box is exactly what let outside clicks fall
    // through to the desktop and left the menu undismissable.
    expect(setMenuHitbox).toHaveBeenCalledWith({
      x: 0,
      y: 0,
      w: window.innerWidth,
      h: window.innerHeight,
    })
  })

  it('renders a transparent full-viewport backdrop, and clicking it dismisses the menu', () => {
    const onClose = vi.fn()
    const { container } = render(
      <ContextMenu x={10} y={10} items={items} reportHitbox onAction={() => {}} onClose={onClose} />,
    )
    const backdrop = container.querySelector('.cc-menu-backdrop') as HTMLElement | null
    expect(backdrop).not.toBeNull()
    // The backdrop is the real DOM element the outside click lands on — without it the
    // overlay's pointer-events:none body would swallow the event before any handler.
    expect(backdrop!.style.pointerEvents).toBe('auto')
    expect(backdrop!.style.background).toBe('transparent')
    fireEvent.mouseDown(backdrop!)
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('clears its hitbox on close, so no stale full-screen rect keeps capturing the screen', () => {
    const setMenuHitbox = vi.spyOn(petBridge, 'setMenuHitbox').mockImplementation(() => {})
    const { unmount } = render(
      <ContextMenu x={10} y={10} items={items} reportHitbox onAction={() => {}} onClose={() => {}} />,
    )
    setMenuHitbox.mockClear()
    unmount()
    expect(setMenuHitbox).toHaveBeenCalledWith(null)
  })

  it('selecting an item still closes the menu and fires its action', () => {
    const onClose = vi.fn()
    const onAction = vi.fn()
    const { getByText } = render(
      <ContextMenu x={10} y={10} items={items} reportHitbox onAction={onAction} onClose={onClose} />,
    )
    fireEvent.click(getByText('Change avatar'))
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(onAction).toHaveBeenCalledWith('gallery')
  })

  it('Escape still closes the menu', () => {
    vi.useFakeTimers()
    try {
      const onClose = vi.fn()
      render(
        <ContextMenu x={10} y={10} items={items} reportHitbox onAction={() => {}} onClose={onClose} />,
      )
      // The key listener is attached one tick after open (so the opening click does
      // not self-close it); step past that guard before dispatching.
      vi.advanceTimersByTime(60)
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
      expect(onClose).toHaveBeenCalledTimes(1)
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('chat context menu — unchanged by the overlay fix', () => {
  it('does not render the overlay backdrop or report a hitbox when reportHitbox is off', () => {
    const setMenuHitbox = vi.spyOn(petBridge, 'setMenuHitbox').mockImplementation(() => {})
    const { container } = render(
      <ContextMenu x={10} y={10} items={items} onAction={() => {}} onClose={() => {}} />,
    )
    expect(container.querySelector('.cc-menu-backdrop')).toBeNull()
    expect(setMenuHitbox).not.toHaveBeenCalled()
  })
})
