import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useState } from 'react'
import { screen, fireEvent, waitFor, act, within } from '@testing-library/react'
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

function Host({ initial = '', inlineToolbarWhileFits = false }: { initial?: string; inlineToolbarWhileFits?: boolean }) {
  const [value, setValue] = useState(initial)
  return <ChatInput value={value} onChange={setValue} onSend={vi.fn()} inlineToolbarWhileFits={inlineToolbarWhileFits} />
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

  it('shows the Esc then Tab hint on screen while the textarea has focus', () => {
    renderWithProviders(<Host />)
    const ta = typeWithCaret('- item|')
    fireEvent.blur(ta)
    expect(screen.getByTestId('list-indent-hint').className).toContain('sr-only')
    fireEvent.focus(ta)
    const hint = screen.getByTestId('list-indent-hint')
    expect(hint.className).not.toContain('sr-only')
    expect(hint.textContent).toMatch(/Escape, then Tab/)
  })

  it('Dismiss hides the visible hint, keeps it for screen readers, and persists', () => {
    const { unmount } = renderWithProviders(<Host />)
    const ta = typeWithCaret('- item|')
    const dismiss = screen.getByTestId('list-indent-hint-dismiss')
    // A pointer press must not blur the textarea, or the button unmounts first.
    expect(fireEvent.mouseDown(dismiss)).toBe(false)
    // Nothing is stored before the click, so no mount default can reach the host backup.
    expect(localStorage.getItem('kirocrew:list-indent-hint-dismissed')).toBeNull()
    fireEvent.click(dismiss)
    expect(localStorage.getItem('kirocrew:list-indent-hint-dismissed')).toBe('1')
    // Marked as an explicit choice, so a host restore cannot overwrite it.
    expect(JSON.parse(localStorage.getItem('mc-ui-prefs-plain-dirty') ?? '[]')).toContain('kirocrew:list-indent-hint-dismissed')
    expect(document.activeElement).toBe(ta)
    expect(screen.getByTestId('list-indent-hint').className).toContain('sr-only')
    expect(screen.queryByTestId('list-indent-hint-dismiss')).toBeNull()
    expect(ta.getAttribute('aria-describedby')).toBeTruthy()
    unmount()
    renderWithProviders(<Host />)
    typeWithCaret('- item|')
    expect(screen.getByTestId('list-indent-hint').className).toContain('sr-only')
  })

  it('an already-mounted sibling composer picks up dismissal on focus', () => {
    renderWithProviders(
      <>
        <div data-testid="first-composer"><Host initial="- first" /></div>
        <div data-testid="second-composer"><Host initial="- second" /></div>
      </>,
    )
    const first = within(screen.getByTestId('first-composer'))
    const second = within(screen.getByTestId('second-composer'))

    fireEvent.focus(first.getByLabelText('Message input'))
    fireEvent.click(first.getByTestId('list-indent-hint-dismiss'))
    fireEvent.focus(second.getByLabelText('Message input'))

    expect(second.getByTestId('list-indent-hint').className).toContain('sr-only')
    expect(second.queryByTestId('list-indent-hint-dismiss')).toBeNull()
  })

  it('keeps the hint visible and reports an explicit save the browser refuses', () => {
    renderWithProviders(<Host />)
    typeWithCaret('- item|')
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('Storage quota exceeded', 'QuotaExceededError')
    })
    try {
      fireEvent.click(screen.getByTestId('list-indent-hint-dismiss'))
      expect(screen.getByTestId('list-indent-hint').className).not.toContain('sr-only')
      expect(screen.getByTestId('list-indent-hint-dismiss')).toBeInTheDocument()
      expect(screen.getByTestId('list-indent-hint-save-error')).toHaveTextContent(/not saved/i)
      expect(localStorage.getItem('kirocrew:list-indent-hint-dismissed')).toBeNull()
    } finally {
      setItem.mockRestore()
    }
  })

  it('clears textarea focus when a one-row hint has no Dismiss target', () => {
    renderWithProviders(<Host initial="- item" inlineToolbarWhileFits />)
    const ta = input()
    fireEvent.focus(ta)
    expect(screen.queryByTestId('list-indent-hint-dismiss')).toBeNull()
    fireEvent.blur(ta, { relatedTarget: null })
    fireEvent.change(ta, { target: { value: '- item\n- next' } })
    expect(screen.getByTestId('list-indent-hint').className).toContain('sr-only')
  })

  it('Tab from the textarea to Dismiss keeps the hint mounted', () => {
    renderWithProviders(<Host />)
    const ta = typeWithCaret('- item|')
    const dismiss = screen.getByTestId('list-indent-hint-dismiss')
    fireEvent.blur(ta, { relatedTarget: dismiss })
    expect(screen.getByTestId('list-indent-hint-dismiss')).toBe(dismiss)
    fireEvent.blur(dismiss, { relatedTarget: document.body })
    expect(screen.queryByTestId('list-indent-hint-dismiss')).toBeNull()
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
