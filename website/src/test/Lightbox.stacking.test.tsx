import { render, fireEvent, act } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Lightbox } from '../components/markdown/Lightbox'
import { OverlayPortal } from '../components/overlayRoot'

// The viewer can open from inside an opaque full-screen shell at z-[9999] (an
// image artifact in the side panel's full screen, #28760). It stays on that
// same z-[9999] tier and is portalled when it opens, so DOM order decides: it
// paints over the shell that opened it, and a palette or menu opened after it
// (also z-[9999] portals) still paints over the viewer. jsdom does no stacking,
// so the classes and the DOM order are the observable facts.

const open = () => window.dispatchEvent(new CustomEvent('lightbox', {
  detail: { images: [{ src: '/api/artifacts/diagram/asset', alt: 'Architecture diagram' }], index: 0 },
}))

const zOf = (el: Element | null) => Number(/(?:^|\s)z-\[(\d+)\]/.exec(el?.className ?? '')?.[1] ?? NaN)
const follows = (a: Node, b: Node) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING)

afterEach(() => { vi.unstubAllGlobals() })

describe('Lightbox stacking', () => {
  it('opens after a z-[9999] full-screen shell, on the same tier', () => {
    const { getByTestId } = render(
      <>
        <OverlayPortal><div data-testid="shell" className="fixed inset-0 z-[9999]" /></OverlayPortal>
        <Lightbox />
      </>,
    )
    act(() => { open() })
    const overlay = document.querySelector('[data-testid="lightbox-toolbar"]')?.parentElement as HTMLElement
    expect(overlay).toBeTruthy()
    expect(follows(getByTestId('shell'), overlay)).toBe(true)
    // Not above the tier: a z-[9999] command palette opened later stays on top.
    expect(zOf(overlay)).toBe(9999)
  })

  it('opens its actions menu after the viewer, on the same tier', () => {
    // The menu is only drawn where the image can be copied.
    class FakeClipboardItem { constructor(public items: Record<string, unknown>) {} }
    vi.stubGlobal('ClipboardItem', FakeClipboardItem)
    vi.stubGlobal('navigator', { ...navigator, clipboard: { write: vi.fn(async () => {}) } })
    render(<Lightbox />)
    act(() => { open() })
    const trigger = document.querySelector('[data-testid="lightbox-actions-menu"]') as HTMLElement
    act(() => {
      fireEvent.pointerDown(trigger, { button: 0 })
      fireEvent.pointerUp(trigger)
      fireEvent.click(trigger)
    })
    const menu = document.body.querySelector('[role="menu"]') as HTMLElement
    const overlay = trigger.closest('[role="button"]') as HTMLElement
    expect(menu).not.toBeNull()
    expect(zOf(menu)).toBeGreaterThanOrEqual(zOf(overlay))
    expect(follows(overlay, menu)).toBe(true)
  })
})
