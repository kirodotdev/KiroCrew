import { render, fireEvent, act } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Lightbox } from '../components/markdown/Lightbox'

// Escape on the viewer's open actions menu closes only the menu. A host that
// listens for Escape on the document without checking defaultPrevented (the
// artifact panel, which exits full screen or closes the artifact) must not see
// that key, or one Escape tears down the artifact behind the viewer.

const open = () => window.dispatchEvent(new CustomEvent('lightbox', {
  detail: { images: [{ src: '/x.png', alt: 'x' }], index: 0 },
}))

afterEach(() => { vi.unstubAllGlobals() })

describe('Lightbox actions menu Escape', () => {
  it('closes the menu without reaching a document Escape handler', () => {
    // The menu is only drawn where the image can be copied.
    class FakeClipboardItem { constructor(public items: Record<string, unknown>) {} }
    vi.stubGlobal('ClipboardItem', FakeClipboardItem)
    vi.stubGlobal('navigator', { ...navigator, clipboard: { write: vi.fn(async () => {}) } })
    render(<Lightbox />); const container = document.body
    act(() => { open() })
    const trigger = container.querySelector('[data-testid="lightbox-actions-menu"]') as HTMLElement
    // Radix opens the menu on the pointer gesture, not a bare click, in jsdom.
    act(() => {
      fireEvent.pointerDown(trigger, { button: 0 })
      fireEvent.pointerUp(trigger)
      fireEvent.click(trigger)
    })
    const item = document.body.querySelector('[data-testid="lightbox-copy-image"]') as HTMLElement
    expect(item).not.toBeNull()

    const hostEscape = vi.fn()
    const onDocKey = (e: KeyboardEvent) => { if (e.key === 'Escape') hostEscape() }
    document.addEventListener('keydown', onDocKey)
    try {
      act(() => { fireEvent.keyDown(item, { key: 'Escape' }) })
      expect(document.body.querySelector('[role="menu"]')).toBeNull()
      expect(hostEscape).not.toHaveBeenCalled()
      // The viewer itself stays open: only the menu closed.
      expect(container.querySelector('[data-testid="lightbox-actions-menu"]')).not.toBeNull()
    } finally {
      document.removeEventListener('keydown', onDocKey)
    }
  })
})
