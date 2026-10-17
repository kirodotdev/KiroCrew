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
})
