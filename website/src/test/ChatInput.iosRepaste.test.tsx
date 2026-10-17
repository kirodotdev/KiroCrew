import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { useState } from 'react'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { isKeyboardRepaste, REPASTE_KEY_WINDOW_MS } from '../components/chat-input/paste'

/**
 * The iOS 27 keyboard's one-tap Paste stays armed after it lands in a web
 * textarea, and the next space accepts it again: keydown (keyCode 229), then a
 * second `paste` with the same text ~30ms later, then the space's insertText.
 * The event order and timings here are the ones recorded on an iPhone.
 */

const URL_TEXT = 'https://github.com/kirodotdev/KiroCrew/pulls'

function clipboard(text: string) {
  return { clipboardData: { types: ['text/plain'], items: [], getData: () => text } }
}

function Harness() {
  const [value, setValue] = useState('')
  return <ChatInput value={value} onChange={setValue} onSend={vi.fn()} />
}

/** jsdom has no default paste action, so do what the browser would. */
function nativePaste(textarea: HTMLTextAreaElement, text: string): boolean {
  const notCancelled = fireEvent.paste(textarea, clipboard(text))
  if (notCancelled) {
    const start = textarea.selectionStart
    const next = textarea.value.slice(0, start) + text + textarea.value.slice(textarea.selectionEnd)
    fireEvent.change(textarea, { target: { value: next, selectionStart: start + text.length, selectionEnd: start + text.length } })
    textarea.setSelectionRange(start + text.length, start + text.length)
  }
  return notCancelled
}

describe('isKeyboardRepaste', () => {
  const last = { text: URL_TEXT, inserted: URL_TEXT, at: 1000 }
  const base = { last, text: URL_TEXT, keyDown: { keyCode: 229, at: 1900 }, now: 1930, valueBeforeCaret: URL_TEXT }

  it('flags the replay a space triggers', () => {
    expect(isKeyboardRepaste(base)).toBe(true)
  })

  it('lets a different text through', () => {
    expect(isKeyboardRepaste({ ...base, text: 'other' })).toBe(false)
  })

  it('lets a second paste with no keyboard key in between through', () => {
    expect(isKeyboardRepaste({ ...base, keyDown: { keyCode: 229, at: 900 } })).toBe(false)
    expect(isKeyboardRepaste({ ...base, keyDown: null })).toBe(false)
  })

  it('lets a paste long after the key through', () => {
    expect(isKeyboardRepaste({ ...base, now: 1900 + REPASTE_KEY_WINDOW_MS + 1 })).toBe(false)
  })

  it('lets a hardware-key paste through', () => {
    expect(isKeyboardRepaste({ ...base, keyDown: { keyCode: 86, at: 1900 } })).toBe(false)
  })

  it('lets it through once the caret moved off the first paste', () => {
    expect(isKeyboardRepaste({ ...base, valueBeforeCaret: `${URL_TEXT} more` })).toBe(false)
  })
})

describe('ChatInput drops the iOS suggestion-bar replay', () => {
  // The guard reads `performance.now()`; pin it so each event happens at the
  // instant the test names, at the gaps recorded on device.
  let clock = 0
  beforeEach(() => {
    clock = 1000
    vi.spyOn(performance, 'now').mockImplementation(() => clock)
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('keeps one copy when a space follows a one-tap Paste', () => {
    renderWithProviders(<Harness />)
    const textarea = screen.getByRole('textbox') as HTMLTextAreaElement
    expect(nativePaste(textarea, URL_TEXT)).toBe(true)
    expect(textarea.value).toBe(URL_TEXT)
    clock = 1900
    fireEvent.keyDown(textarea, { key: ' ', keyCode: 229 })
    clock = 1930
    expect(nativePaste(textarea, URL_TEXT)).toBe(false)
    expect(textarea.value).toBe(URL_TEXT)
  })

  it('still pastes twice when the user really pastes twice', () => {
    renderWithProviders(<Harness />)
    const textarea = screen.getByRole('textbox') as HTMLTextAreaElement
    nativePaste(textarea, URL_TEXT)
    clock = 1930
    expect(nativePaste(textarea, URL_TEXT)).toBe(true)
    expect(textarea.value).toBe(URL_TEXT + URL_TEXT)
  })
})
