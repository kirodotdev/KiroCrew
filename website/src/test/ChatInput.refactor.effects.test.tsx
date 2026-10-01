import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, renderHook } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { usePromptHistory } from '../components/chat-input/draftHistory'

/* ── The composer's send-clear and slot-switch effects list `closePickers` and
 *    `promptHistory` as dependencies, and fire only on a value or slot change
 *    because both keep one identity for the composer's lifetime. If either one
 *    changed per render, the slot-switch effect would run on every re-render
 *    and end prompt-history browsing (and close an open picker) mid-gesture. ── */

// A fresh array each render, as a parent that rebuilds the list would pass.
const sent = () => [{ text: 'first' }, { text: 'second' }, { text: 'third' }]

beforeEach(() => {
  localStorage.clear()
})

describe('ChatInput effects across an unrelated re-render', () => {
  it('usePromptHistory keeps one identity across renders', () => {
    const { result, rerender } = renderHook(() => usePromptHistory())
    const first = result.current
    rerender()
    expect(result.current).toBe(first)
  })

  it('keeps prompt-history browsing on its entry when an unrelated prop changes', () => {
    const onChange = vi.fn()
    const props = { onChange, onSend: vi.fn(), contextPct: 10 }
    const { rerender } = renderWithProviders(<ChatInput {...props} value="" sentMessages={sent()} />)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.keyDown(ta, { key: 'ArrowUp' })
    expect(onChange).toHaveBeenLastCalledWith('third')
    rerender(<ChatInput {...props} value="third" sentMessages={sent()} />)
    // Nothing the history reads changes here: a context-usage tick re-renders
    // the composer with the same slot and the same text.
    rerender(<ChatInput {...props} contextPct={42} value="third" sentMessages={sent()} />)
    ta.setSelectionRange(0, 0)
    fireEvent.keyDown(ta, { key: 'ArrowUp' })
    expect(onChange).toHaveBeenLastCalledWith('second')
  })
})
