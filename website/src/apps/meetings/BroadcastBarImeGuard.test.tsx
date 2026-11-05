/**
 * IME Enter guard on the Meetings broadcast bar, a single-line Enter-only site
 * routed through `useImeGuard`'s `bindEnter`.
 *
 * On WebKit the keydown that commits an IME candidate is dispatched AFTER
 * `compositionend`, where `KeyboardEvent.isComposing` already reads false. The
 * hook's 50ms latch is the only signal that survives into that window, so each
 * test arms the latch with compositionStart→compositionEnd and then fires the
 * committing Enter exactly as WebKit delivers it (native flag clear). The core
 * sites of the same guard are pinned in `src/test/ImeEnterGuardSites.test.tsx`.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

import BroadcastBar from './components/BroadcastBar'

/** Arm the hook's post-composition latch: the WebKit commit-Enter window. */
function armLatch(el: Element) {
  fireEvent.compositionStart(el)
  fireEvent.compositionEnd(el)
}

describe('BroadcastBar input — rule 1 single-line Enter-only site: bindEnter', () => {
  // A bindEnter site: the handler is Enter-only, so the whole binding
  // (composition tracking + claim + focus/blur recovery) comes from one spread
  // and no hand-rolled spelling exists to copy.
  it('does NOT send on the committing Enter in the post-composition window', () => {
    const onSend = vi.fn()
    render(<BroadcastBar onSend={onSend} />)
    const input = screen.getByLabelText(/./, { selector: 'input' }) as HTMLInputElement
    fireEvent.change(input, { target: { value: '你好' } })
    armLatch(input)
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onSend).not.toHaveBeenCalled()
  })

  it('leaves a mid-composition Enter to the IME (native flag set)', () => {
    const onSend = vi.fn()
    render(<BroadcastBar onSend={onSend} />)
    const input = screen.getByLabelText(/./, { selector: 'input' }) as HTMLInputElement
    fireEvent.change(input, { target: { value: '你好' } })
    fireEvent.compositionStart(input)
    const defaultNotPrevented = fireEvent.keyDown(input, { key: 'Enter', isComposing: true })
    expect(onSend).not.toHaveBeenCalled()
    // The guard must not touch a key the IME is consuming.
    expect(defaultNotPrevented).toBe(true)
  })

  it('sends on a plain Enter (positive control)', () => {
    const onSend = vi.fn()
    render(<BroadcastBar onSend={onSend} />)
    const input = screen.getByLabelText(/./, { selector: 'input' }) as HTMLInputElement
    fireEvent.change(input, { target: { value: 'hello' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onSend).toHaveBeenCalledWith('hello')
  })

  it('a stale latch from an abandoned composition clears on the next focus', () => {
    // The latch reset rides in the binding's onFocus, so the site needs no
    // `onFocus={() => ime.reset()}` of its own. Abandon a composition (no
    // compositionend), refocus, and the next Enter must send.
    const onSend = vi.fn()
    render(<BroadcastBar onSend={onSend} />)
    const input = screen.getByLabelText(/./, { selector: 'input' }) as HTMLInputElement
    fireEvent.change(input, { target: { value: 'hello' } })
    fireEvent.compositionStart(input)   // abandoned: no compositionEnd follows
    fireEvent.focus(input)
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onSend).toHaveBeenCalledWith('hello')
  })
})
