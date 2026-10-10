import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useState } from 'react'
import { screen, fireEvent, waitFor, act } from '@testing-library/react'
import { renderWithProviders } from './helpers'

/* ── Tab / Shift+Tab list indent in the textarea composer (the live one). ── */
const mockApi = vi.hoisted(() => ({
  skills: vi.fn(),
  skillTrust: vi.fn(),
  grantSkillTrust: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

import ChatInput from '../components/ChatInput'

beforeEach(() => {
  vi.restoreAllMocks()
  vi.clearAllMocks()
  localStorage.clear()
  mockApi.skills.mockResolvedValue([])
  mockApi.skillTrust.mockResolvedValue({ project: '/p', project_key: '/p' })
  mockApi.grantSkillTrust.mockResolvedValue({ trusted: true })
})

function Host({ initial = '' }: { initial?: string }) {
  const [value, setValue] = useState(initial)
  return <ChatInput value={value} onChange={setValue} onSend={vi.fn()} />
}

function input() {
  return screen.getByLabelText('Message input') as HTMLTextAreaElement
}

/** Type `marked` (caret at `|`) as a real edit, then place the caret. */
function typeWithCaret(marked: string) {
  const caret = marked.indexOf('|')
  const value = marked.replace('|', '')
  const ta = input()
  ta.focus()
  fireEvent.change(ta, { target: { value } })
  ta.setSelectionRange(caret, caret)
  return ta
}

/** Returns whether the key was left to the browser (not preventDefault-ed). */
function tab(ta: HTMLTextAreaElement, init: KeyboardEventInit = {}) {
  let passed = true
  act(() => { passed = fireEvent.keyDown(ta, { key: 'Tab', ...init }) })
  return passed
}

async function flushFrame() {
  await act(async () => { await new Promise(resolve => requestAnimationFrame(() => resolve(undefined))) })
}

describe('ChatInput: Tab indents list lines', () => {
  it('Tab on a list line adds two spaces and keeps the caret on its character', async () => {
    renderWithProviders(<Host />)
    const ta = typeWithCaret('intro\n- ite|m')
    expect(tab(ta)).toBe(false)
    expect(ta.value).toBe('intro\n  - item')
    await flushFrame()
    expect(ta.selectionStart).toBe('intro\n  - ite'.length)
    expect(ta.selectionEnd).toBe(ta.selectionStart)
  })

  it('Shift+Tab on an indented list line removes one level', async () => {
    renderWithProviders(<Host />)
    const ta = typeWithCaret('    1. dee|p')
    expect(tab(ta, { shiftKey: true })).toBe(false)
    expect(ta.value).toBe('  1. deep')
  })

  it('Esc then Tab on a list line moves focus instead of indenting', () => {
    renderWithProviders(<Host />)
    const ta = typeWithCaret('- ite|m')
    act(() => { fireEvent.keyDown(ta, { key: 'Escape' }) })
    expect(tab(ta)).toBe(true)
    expect(ta.value).toBe('- item')
    // The escape covers one Tab only; the next one indents again.
    expect(tab(ta)).toBe(false)
    expect(ta.value).toBe('  - item')
  })

  it('a key between Esc and Tab spends the escape, a lone Shift does not', () => {
    renderWithProviders(<Host />)
    const ta = typeWithCaret('- ite|m')
    act(() => { fireEvent.keyDown(ta, { key: 'Escape' }) })
    act(() => { fireEvent.keyDown(ta, { key: 'ArrowRight' }) })
    expect(tab(ta)).toBe(false)
    expect(ta.value).toBe('  - item')
    act(() => { fireEvent.keyDown(ta, { key: 'Escape' }) })
    act(() => { fireEvent.keyDown(ta, { key: 'Shift', shiftKey: true }) })
    expect(tab(ta, { shiftKey: true })).toBe(true)
    expect(ta.value).toBe('  - item')
  })

  it('tells screen readers about Esc then Tab only while the draft has a list line', () => {
    renderWithProviders(<Host />)
    const ta = typeWithCaret('plain|')
    expect(ta.getAttribute('aria-describedby')).toBeNull()
    typeWithCaret('- item|')
    const ids = ta.getAttribute('aria-describedby') ?? ''
    expect(ids).not.toBe('')
    expect(document.getElementById(ids)?.textContent).toMatch(/Escape, then Tab/)
  })

  it('Tab on a plain line is left alone so focus moves', () => {
    renderWithProviders(<Host />)
    const ta = typeWithCaret('plain te|xt')
    expect(tab(ta)).toBe(true)
    expect(ta.value).toBe('plain text')
  })

  it('Shift+Tab on an unindented list line is left alone so focus moves', () => {
    renderWithProviders(<Host />)
    const ta = typeWithCaret('- to|p')
    expect(tab(ta, { shiftKey: true })).toBe(true)
    expect(ta.value).toBe('- top')
  })

  it('a ranged selection is left alone', () => {
    renderWithProviders(<Host />)
    const ta = typeWithCaret('- item|')
    ta.setSelectionRange(2, 6)
    expect(tab(ta)).toBe(true)
    expect(ta.value).toBe('- item')
  })

  it('Tab during IME composition is left alone', () => {
    renderWithProviders(<Host />)
    const ta = typeWithCaret('- ite|m')
    fireEvent.compositionStart(ta)
    expect(tab(ta, { isComposing: true, keyCode: 229 })).toBe(true)
    expect(ta.value).toBe('- item')
  })

  it('the WebKit commit Tab after compositionend does not indent or move focus', () => {
    renderWithProviders(<Host />)
    const ta = typeWithCaret('- ite|m')
    fireEvent.compositionStart(ta)
    fireEvent.compositionEnd(ta)
    // isComposing is already false on this keydown; only the latch knows.
    expect(tab(ta)).toBe(false)
    expect(ta.value).toBe('- item')
  })

  it('Tab while a suggestion menu is open does not indent', async () => {
    renderWithProviders(<Host />)
    const ta = typeWithCaret('- $gr|')
    await waitFor(() => expect(mockApi.skills).toHaveBeenCalled())
    tab(ta)
    expect(ta.value).not.toMatch(/^ {2}/)
  })

  // Tab is a user edit, so it must follow the caret like typing does. It does not
  // arrive through the `input` event, and the value effect alone treats a changed
  // value as parent-set and skips the snap. jsdom has no layout, so stub what
  // applyHeight reads: an overflowing box with a stale scroll offset.
  it('Tab at the end of an overflowing list follows the caret like a typed edit', () => {
    renderWithProviders(<Host />)
    const ta = typeWithCaret('a\nb\nc\nd\ne\nf\ng\nh\n- item|')
    let scrollTop = 71
    Object.defineProperty(ta, 'scrollTop', { configurable: true, get: () => scrollTop, set: v => { scrollTop = v } })
    Object.defineProperty(ta, 'scrollHeight', { configurable: true, get: () => 289 })
    Object.defineProperty(ta, 'clientHeight', { configurable: true, get: () => 140 })
    expect(tab(ta)).toBe(false)
    expect(ta.value.endsWith('\n  - item')).toBe(true)
    expect(ta.scrollTop).toBe(289)
  })

  it('one Ctrl+Z reverts one indent step', async () => {
    renderWithProviders(<Host />)
    const ta = typeWithCaret('- ite|m')
    tab(ta)
    expect(ta.value).toBe('  - item')
    await flushFrame()
    act(() => { fireEvent.keyDown(ta, { key: 'z', ctrlKey: true }) })
    await waitFor(() => expect(ta.value).toBe('- item'))
  })

  it('redo after undoing an indent restores the caret the indent left', async () => {
    renderWithProviders(<Host />)
    const ta = typeWithCaret('- ite|m')
    tab(ta)
    expect(ta.value).toBe('  - item')
    await flushFrame()
    act(() => { fireEvent.keyDown(ta, { key: 'z', ctrlKey: true }) })
    await waitFor(() => expect(ta.value).toBe('- item'))
    await flushFrame()
    act(() => { fireEvent.keyDown(ta, { key: 'z', ctrlKey: true, shiftKey: true }) })
    await waitFor(() => expect(ta.value).toBe('  - item'))
    await flushFrame()
    expect(ta.selectionStart).toBe('  - ite'.length)
    expect(ta.selectionEnd).toBe(ta.selectionStart)
  })
})
