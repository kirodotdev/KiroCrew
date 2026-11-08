import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { stubStripHeights } from './stripHeights'

// The composer's send key comes from Settings -> Chat -> Composer -> Send
// shortcut, stored under `mc-chat-config`. A host that renders ChatInput without
// a `sendOnEnter` prop (the session-grid pane, the side panel) must still honour
// it: falling back to 'enter' there sent a half-written draft on a stray Enter
// for a user who had explicitly chosen Ctrl/Cmd+Enter.

const mobileEnv = vi.hoisted(() => ({ mobile: false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => mobileEnv.mobile }))

const baseProps = { value: 'half-written draft', onChange: vi.fn() }

function seedSendMode(mode: string) {
  localStorage.setItem('mc-chat-config', JSON.stringify({ sendOnEnter: mode }))
}

/** Renders with NO `sendOnEnter` prop — the case every reported host hits. */
function mountWithStoredMode(mode: string, onSend: () => void) {
  seedSendMode(mode)
  renderWithProviders(<ChatInput {...baseProps} onSend={onSend} />)
  return screen.getByLabelText('Message input')
}

beforeEach(() => {
  vi.restoreAllMocks()
  stubStripHeights()
  localStorage.clear()
  mobileEnv.mobile = false
})

describe('composer send mode comes from the stored preference', () => {
  describe("stored 'ctrl-enter'", () => {
    it('plain Enter inserts a newline instead of sending', () => {
      const onSend = vi.fn()
      const ta = mountWithStoredMode('ctrl-enter', onSend)
      // `fireEvent` returns false when the handler called preventDefault, so a
      // true here is the newline: the textarea keeps the key.
      expect(fireEvent.keyDown(ta, { key: 'Enter' })).toBe(true)
      expect(onSend).not.toHaveBeenCalled()
    })

    it('Ctrl+Enter sends', () => {
      const onSend = vi.fn()
      const ta = mountWithStoredMode('ctrl-enter', onSend)
      fireEvent.keyDown(ta, { key: 'Enter', ctrlKey: true })
      expect(onSend).toHaveBeenCalledOnce()
    })

    it('Meta+Enter sends', () => {
      const onSend = vi.fn()
      const ta = mountWithStoredMode('ctrl-enter', onSend)
      fireEvent.keyDown(ta, { key: 'Enter', metaKey: true })
      expect(onSend).toHaveBeenCalledOnce()
    })

    it('Shift+Enter inserts a newline', () => {
      const onSend = vi.fn()
      const ta = mountWithStoredMode('ctrl-enter', onSend)
      expect(fireEvent.keyDown(ta, { key: 'Enter', shiftKey: true })).toBe(true)
      expect(onSend).not.toHaveBeenCalled()
    })
  })

  describe("stored 'enter' (unchanged behaviour)", () => {
    it('plain Enter sends', () => {
      const onSend = vi.fn()
      const ta = mountWithStoredMode('enter', onSend)
      fireEvent.keyDown(ta, { key: 'Enter' })
      expect(onSend).toHaveBeenCalledOnce()
    })

    it('Ctrl+Enter sends', () => {
      const onSend = vi.fn()
      const ta = mountWithStoredMode('enter', onSend)
      fireEvent.keyDown(ta, { key: 'Enter', ctrlKey: true })
      expect(onSend).toHaveBeenCalledOnce()
    })

    it('Meta+Enter sends', () => {
      const onSend = vi.fn()
      const ta = mountWithStoredMode('enter', onSend)
      fireEvent.keyDown(ta, { key: 'Enter', metaKey: true })
      expect(onSend).toHaveBeenCalledOnce()
    })

    it('Shift+Enter inserts a newline', () => {
      const onSend = vi.fn()
      const ta = mountWithStoredMode('enter', onSend)
      expect(fireEvent.keyDown(ta, { key: 'Enter', shiftKey: true })).toBe(true)
      expect(onSend).not.toHaveBeenCalled()
    })
  })

  describe("stored 'enter-ctrl-newline'", () => {
    it('plain Enter sends and Ctrl+Enter inserts a newline', () => {
      const onSend = vi.fn()
      const onChange = vi.fn()
      seedSendMode('enter-ctrl-newline')
      renderWithProviders(<ChatInput {...baseProps} onChange={onChange} onSend={onSend} />)
      const ta = screen.getByLabelText('Message input')
      fireEvent.keyDown(ta, { key: 'Enter', ctrlKey: true })
      expect(onSend).not.toHaveBeenCalled()
      expect(onChange).toHaveBeenCalled()
      fireEvent.keyDown(ta, { key: 'Enter' })
      expect(onSend).toHaveBeenCalledOnce()
    })
  })

  it('an explicit prop still overrides the stored preference', () => {
    const onSend = vi.fn()
    seedSendMode('enter')
    renderWithProviders(<ChatInput {...baseProps} onSend={onSend} sendOnEnter="ctrl-enter" />)
    const ta = screen.getByLabelText('Message input')
    fireEvent.keyDown(ta, { key: 'Enter' })
    expect(onSend).not.toHaveBeenCalled()
    fireEvent.keyDown(ta, { key: 'Enter', ctrlKey: true })
    expect(onSend).toHaveBeenCalledOnce()
  })

  it('picks up a change to the preference without a remount', () => {
    const onSend = vi.fn()
    const ta = mountWithStoredMode('enter', onSend)
    seedSendMode('ctrl-enter')
    fireEvent(window, new Event('mc-config-changed'))
    expect(fireEvent.keyDown(ta, { key: 'Enter' })).toBe(true)
    expect(onSend).not.toHaveBeenCalled()
  })

  // Regression for #14725: a host that opts out of the optimize affordance
  // (`promptOptimizer={false}`, e.g. the side panel) has no optimize branch to
  // claim Cmd/Ctrl+Shift+Enter, so the chord must fall through to a NO-OP — it
  // must never satisfy the `ctrl-enter` send binding and submit the draft the
  // user was still editing. The optimize chord shares a modifier with the
  // `ctrl-enter` send key, so the send key must exclude Shift.
  describe("the optimize chord does not send when promptOptimizer is off", () => {
    function mountNoOptimizer(mode: string, onSend: () => void) {
      seedSendMode(mode)
      renderWithProviders(<ChatInput {...baseProps} onSend={onSend} promptOptimizer={false} />)
      return screen.getByLabelText('Message input')
    }

    it("ctrl-enter: Ctrl+Shift+Enter neither sends nor newlines (no-op)", () => {
      const onSend = vi.fn()
      const ta = mountNoOptimizer('ctrl-enter', onSend)
      // fireEvent returns false when the handler called preventDefault; a true
      // here means nobody consumed the chord, so it is a plain no-op.
      expect(fireEvent.keyDown(ta, { key: 'Enter', ctrlKey: true, shiftKey: true })).toBe(true)
      expect(onSend).not.toHaveBeenCalled()
    })

    it("ctrl-enter: Cmd+Shift+Enter neither sends nor newlines (no-op)", () => {
      const onSend = vi.fn()
      const ta = mountNoOptimizer('ctrl-enter', onSend)
      expect(fireEvent.keyDown(ta, { key: 'Enter', metaKey: true, shiftKey: true })).toBe(true)
      expect(onSend).not.toHaveBeenCalled()
    })

    it("ctrl-enter: a plain Ctrl+Enter still sends (no Shift)", () => {
      const onSend = vi.fn()
      const ta = mountNoOptimizer('ctrl-enter', onSend)
      fireEvent.keyDown(ta, { key: 'Enter', ctrlKey: true })
      expect(onSend).toHaveBeenCalledOnce()
    })

    it("enter: Ctrl+Shift+Enter does not send (Shift already excluded)", () => {
      const onSend = vi.fn()
      const ta = mountNoOptimizer('enter', onSend)
      expect(fireEvent.keyDown(ta, { key: 'Enter', ctrlKey: true, shiftKey: true })).toBe(true)
      expect(onSend).not.toHaveBeenCalled()
    })
  })

  // With the optimize affordance present (the default), the same chord is
  // claimed by the optimize branch and still must not reach the send path.
  it("ctrl-enter: Ctrl+Shift+Enter optimizes (not sends) when promptOptimizer is on", () => {
    const onSend = vi.fn()
    const ta = mountWithStoredMode('ctrl-enter', onSend)
    // The optimize branch calls preventDefault, so fireEvent returns false.
    expect(fireEvent.keyDown(ta, { key: 'Enter', ctrlKey: true, shiftKey: true })).toBe(false)
    expect(onSend).not.toHaveBeenCalled()
  })
})
