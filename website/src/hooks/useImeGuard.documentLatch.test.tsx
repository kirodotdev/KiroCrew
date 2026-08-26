import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, fireEvent, cleanup } from '@testing-library/react'
import { useDocumentImeLatch } from './useImeGuard'
import type { ImeLatch } from './useImeGuard'

/**
 * The document-tracked IME latch shared by every focus-boundary Tab trap
 * (`useDialogFocusTrap` plus the dialogs and inline traps that hand-roll the
 * same shape). These tests pin the seam itself — the tracking lifecycle, the
 * post-composition window, the stranded-latch recovery, and the
 * enabled-keyed reset — so each consuming site only needs a wiring test.
 */

function Harness({
  enabled = true,
  expose,
}: {
  enabled?: boolean
  expose: (latch: ImeLatch) => void
}) {
  expose(useDocumentImeLatch(enabled))
  return <input data-testid="field" aria-label="composition host" />
}

function mount(enabled = true) {
  let latch!: ImeLatch
  const utils = render(<Harness enabled={enabled} expose={(l) => { latch = l }} />)
  return { ...utils, latch: () => latch, field: utils.getByTestId('field') }
}

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('useDocumentImeLatch', () => {
  it('latches on a composition anywhere in the document', () => {
    const { latch, field } = mount()
    expect(latch().isLatched()).toBe(false)
    fireEvent.compositionStart(field)
    expect(latch().isLatched()).toBe(true)
  })

  it('stays latched through the post-composition window', () => {
    // On WebKit the keydown that commits a candidate arrives AFTER
    // compositionend with `isComposing` already false — only the tracked
    // window can identify it.
    const { latch, field } = mount()
    fireEvent.compositionStart(field)
    fireEvent.compositionEnd(field)
    expect(latch().isLatched()).toBe(true)
  })

  it('unlatches once the post-composition window elapses', () => {
    vi.useFakeTimers()
    const { latch, field } = mount()
    fireEvent.compositionStart(field)
    fireEvent.compositionEnd(field)
    vi.advanceTimersByTime(60)
    expect(latch().isLatched()).toBe(false)
  })

  it('recovers from an abandoned composition on focusout', () => {
    // No compositionend ever fires (OS-level IME cancel, focus stolen
    // mid-composition). Without the recovery the latch would stay set and
    // consume every later boundary Tab for the surface's lifetime.
    const { latch, field } = mount()
    field.focus()
    fireEvent.compositionStart(field)
    expect(latch().isLatched()).toBe(true)
    field.blur()
    expect(latch().isLatched()).toBe(false)
  })

  it('does not track while disabled', () => {
    const { latch, field } = mount(false)
    fireEvent.compositionStart(field)
    expect(latch().isLatched()).toBe(false)
  })

  it('does not inherit a stale latch across a disable/re-enable', () => {
    let latch!: ImeLatch
    const expose = (l: ImeLatch) => { latch = l }
    const { rerender, getByTestId } = render(<Harness enabled expose={expose} />)
    fireEvent.compositionStart(getByTestId('field'))
    expect(latch.isLatched()).toBe(true)
    rerender(<Harness enabled={false} expose={expose} />)
    rerender(<Harness enabled expose={expose} />)
    expect(latch.isLatched()).toBe(false)
  })

  it('claimKey declines and consumes a latched key, accepts a clear one', () => {
    const { latch, field } = mount()
    fireEvent.compositionStart(field)
    fireEvent.compositionEnd(field)
    // Post-composition window: both native signals read clear, so the claim
    // consumes the key (preventDefault + stopPropagation) and declines.
    const latched = new KeyboardEvent('keydown', { key: 'Tab', cancelable: true, bubbles: true })
    expect(latch().claimKey(latched)).toBe(false)
    expect(latched.defaultPrevented).toBe(true)
    latch().reset()
    const clear = new KeyboardEvent('keydown', { key: 'Tab', cancelable: true, bubbles: true })
    expect(latch().claimKey(clear)).toBe(true)
    expect(clear.defaultPrevented).toBe(false)
  })
})
