import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import PrivacyChapter from './PrivacyChapter'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      patchConfig: vi.fn().mockResolvedValue({}),
      themeBoot: vi.fn().mockResolvedValue({ mode: '', color: '', onboarded: false }),
      beaconStatus: vi.fn().mockResolvedValue({
        enabled: true,
        would_send: true,
        reason: 'ready',
        endpoint_configured: true,
        env_override: false,
        env_var: 'KIROCREW_TELEMETRY_DISABLED',
      }),
    },
  }
})

/** Every focusable the trap can land on, in the order the trap computes. */
function focusables(): HTMLElement[] {
  const dialog = document.querySelector('[role="dialog"]') ?? document.body
  return Array.from(
    dialog.querySelectorAll<HTMLElement>(
      'button:not([disabled]), [href], input:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  ).filter(el => el.getAttribute('aria-hidden') !== 'true')
}

describe('PrivacyChapter Tab trap', () => {
  it('focuses the heading on open', async () => {
    renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)
    const heading = await screen.findByRole('heading', { name: 'Privacy' })
    expect(document.activeElement).toBe(heading)
  })

  it('Tab from outside the ring wraps to the first focusable', async () => {
    renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)
    await screen.findByRole('switch', { name: 'Send anonymous usage heartbeat' })
    const ring = focusables()
    expect(ring.length).toBeGreaterThan(0)

    // The heading is tabindex=-1, so it is NOT in the ring: activeIndex is -1.
    fireEvent.keyDown(document, { key: 'Tab' })
    expect(document.activeElement).toBe(ring[0])
  })

  it('Tab off the last focusable wraps back to the first', async () => {
    renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)
    await screen.findByRole('switch', { name: 'Send anonymous usage heartbeat' })
    const ring = focusables()

    ring[ring.length - 1].focus()
    fireEvent.keyDown(document, { key: 'Tab' })
    expect(document.activeElement).toBe(ring[0])
  })

  it('Shift+Tab off the first focusable wraps to the last', async () => {
    renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)
    await screen.findByRole('switch', { name: 'Send anonymous usage heartbeat' })
    const ring = focusables()

    ring[0].focus()
    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(ring[ring.length - 1])
  })

  it('a non-Tab key is ignored by the trap', async () => {
    renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)
    await screen.findByRole('switch', { name: 'Send anonymous usage heartbeat' })
    const ring = focusables()
    ring[0].focus()
    fireEvent.keyDown(document, { key: 'ArrowDown' })
    expect(document.activeElement).toBe(ring[0])
  })

  it('declines a boundary Tab that belongs to an IME composition', async () => {
    // On WebKit the keydown that commits a candidate arrives AFTER
    // compositionend with `isComposing` already false — unguarded, the trap
    // would yank focus and abort the composition (issue class of the shared
    // hook's own guard).
    renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)
    // Wait for the toggle to leave its loading-disabled state so the ring
    // holds more than one member — a one-element ring wraps onto itself and
    // cannot distinguish a decline from a wrap.
    const toggle = await screen.findByRole('switch', { name: 'Send anonymous usage heartbeat' })
    await waitFor(() => expect(toggle).toBeEnabled())
    const ring = focusables()
    expect(ring.length).toBeGreaterThan(1)
    const last = ring[ring.length - 1]
    last.focus()
    fireEvent.compositionStart(last)
    fireEvent.compositionEnd(last)
    fireEvent.keyDown(document, { key: 'Tab' })
    expect(document.activeElement).toBe(last)
    expect(document.activeElement).not.toBe(ring[0])
  })

  it('the trap is torn down when the chapter closes', async () => {
    const { rerender } = renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)
    await screen.findByRole('switch', { name: 'Send anonymous usage heartbeat' })
    rerender(<PrivacyChapter open={false} onContinue={vi.fn()} />)

    document.body.focus()
    const event = new KeyboardEvent('keydown', { key: 'Tab', cancelable: true, bubbles: true })
    document.dispatchEvent(event)
    expect(event.defaultPrevented).toBe(false)
  })
})
