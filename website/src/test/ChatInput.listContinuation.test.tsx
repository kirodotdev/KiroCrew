import { useState } from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { stubStripHeights } from './stripHeights'
import type { SendMode } from '../pages/chat/ChatSettings'
import { formatToken, type PasteBlock } from '../utils/pasteTokens'
import { requestComposerExpand } from '../pages/chat/composerFocus'

// The textarea composer is the one every production host renders (the Lexical
// editor is opt-in and off). Its new-line paths all end in a native
// `beforeinput` of type insertLineBreak/insertParagraph -- including a mobile
// virtual keyboard's Return, whose keydown is often `Unidentified` / keyCode
// 229 and so never matches an Enter check. These tests drive that event.

function Host({ initial, sendOnEnter, onSend, blocks = [], collapsible = false }: {
  initial: string
  sendOnEnter: SendMode
  onSend: () => void
  blocks?: PasteBlock[]
  collapsible?: boolean
}) {
  const [value, setValue] = useState(initial)
  return (
    <>
      <ChatInput value={value} onChange={setValue} onSend={onSend} sendOnEnter={sendOnEnter} pasteBlocks={blocks} onPasteBlocksChange={() => {}} collapsible={collapsible} />
      <output data-testid="value">{value}</output>
    </>
  )
}

function mount(initial: string, sendOnEnter: SendMode = 'ctrl-enter', caret = initial.length, blocks: PasteBlock[] = []) {
  const onSend = vi.fn()
  renderWithProviders(<Host initial={initial} sendOnEnter={sendOnEnter} onSend={onSend} blocks={blocks} />)
  const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
  ta.focus()
  ta.setSelectionRange(caret, caret)
  return { ta, onSend, value: () => screen.getByTestId('value').textContent }
}

/** One new-line action as the browser delivers it. Returns true when the
 *  composer cancelled the native line break (it made its own edit). */
function newLine(ta: HTMLTextAreaElement, inputType = 'insertLineBreak', isComposing = false) {
  const event = new InputEvent('beforeinput', { bubbles: true, cancelable: true, inputType, isComposing })
  act(() => { ta.dispatchEvent(event) })
  return event.defaultPrevented
}

beforeEach(() => {
  vi.restoreAllMocks()
  stubStripHeights()
  localStorage.clear()
})

describe('textarea composer continues markdown lists on a new line', () => {
  it('mobile Return (ctrl-enter mode, keyCode 229 keydown) continues a numbered list', () => {
    const { ta, onSend, value } = mount('1. test')
    // Android Gboard / iOS marked-text Return: the keydown carries no usable key.
    expect(fireEvent.keyDown(ta, { key: 'Unidentified', keyCode: 229 })).toBe(true)
    expect(newLine(ta)).toBe(true)
    expect(value()).toBe('1. test\n2. ')
    expect(ta.selectionStart).toBe('1. test\n2. '.length)
    expect(onSend).not.toHaveBeenCalled()
  })

  it('insertParagraph (some Android keyboards) continues the list too', () => {
    const { ta, value } = mount('9) item')
    expect(newLine(ta, 'insertParagraph')).toBe(true)
    expect(value()).toBe('9) item\n10) ')
  })

  it('continues bullets and moves text after the caret to the new item', () => {
    const bullet = mount('- alpha beta', 'ctrl-enter', '- alpha '.length)
    expect(newLine(bullet.ta)).toBe(true)
    expect(bullet.value()).toBe('- alpha \n- beta')
    expect(bullet.ta.selectionStart).toBe('- alpha \n- '.length)
  })

  it('a new line on an empty item ends the list', () => {
    const { ta, value } = mount('1. test\n2. ')
    expect(newLine(ta)).toBe(true)
    expect(value()).toBe('1. test\n')
  })

  it('one undo reverts the continuation', () => {
    const { ta, value } = mount('1. test')
    expect(newLine(ta)).toBe(true)
    expect(value()).toBe('1. test\n2. ')
    fireEvent.keyDown(ta, { key: 'z', ctrlKey: true })
    expect(value()).toBe('1. test')
  })

  it('keeps a quick list continuation separate from the typing undo burst', () => {
    const { ta, value } = mount('')
    for (const next of ['1', '1.', '1. ', '1. t', '1. te', '1. tes', '1. test']) {
      fireEvent.change(ta, { target: { value: next } })
    }
    expect(newLine(ta)).toBe(true)
    expect(value()).toBe('1. test\n2. ')

    fireEvent.keyDown(ta, { key: 'z', ctrlKey: true })
    expect(value()).toBe('1. test')
  })

  it('an ordinary line keeps the native line break', () => {
    const plain = mount('just text')
    expect(newLine(plain.ta)).toBe(false)
    expect(plain.value()).toBe('just text')
  })

  it('a composing newline is the IME\'s, not a new item', () => {
    const { ta, value } = mount('1. test')
    expect(newLine(ta, 'insertLineBreak', true)).toBe(false)
    expect(value()).toBe('1. test')
  })

  it('a caret inside a paste chip is not split', () => {
    const block: PasteBlock = { id: 'p1', seq: 1, lines: 4, content: 'a\nb\nc\nd' }
    const text = `- ${formatToken(block)}`
    const { ta, value } = mount(text, 'ctrl-enter', 5, [block])
    expect(newLine(ta)).toBe(false)
    expect(value()).toBe(text)
  })

  it('desktop Shift+Enter (enter mode) continues the list; plain Enter still sends', () => {
    const { ta, onSend, value } = mount('1. test', 'enter')
    expect(fireEvent.keyDown(ta, { key: 'Enter', shiftKey: true })).toBe(true)
    expect(newLine(ta)).toBe(true)
    expect(value()).toBe('1. test\n2. ')
    expect(onSend).not.toHaveBeenCalled()
    // The send key is default-prevented at keydown, so no beforeinput follows.
    expect(fireEvent.keyDown(ta, { key: 'Enter' })).toBe(false)
    expect(onSend).toHaveBeenCalledOnce()
  })

  it('Ctrl+Enter in enter-ctrl-newline mode continues the list and does not send', () => {
    const { ta, onSend, value } = mount('- a', 'enter-ctrl-newline')
    expect(fireEvent.keyDown(ta, { key: 'Enter', ctrlKey: true })).toBe(false)
    expect(value()).toBe('- a\n- ')
    expect(onSend).not.toHaveBeenCalled()
  })

  it('Ctrl+Enter during the post-composition latch inserts a plain newline', () => {
    const { ta, onSend, value } = mount('- a', 'enter-ctrl-newline')
    fireEvent.compositionStart(ta)
    fireEvent.compositionEnd(ta)
    expect(fireEvent.keyDown(ta, { key: 'Enter', ctrlKey: true })).toBe(false)
    expect(value()).toBe('- a\n')
    expect(onSend).not.toHaveBeenCalled()
  })

  it('keeps Ctrl+Enter list continuation separate from the typing undo burst', () => {
    const { ta, value } = mount('', 'enter-ctrl-newline')
    for (const next of ['-', '- ', '- a']) fireEvent.change(ta, { target: { value: next } })
    expect(fireEvent.keyDown(ta, { key: 'Enter', ctrlKey: true })).toBe(false)
    expect(value()).toBe('- a\n- ')

    fireEvent.keyDown(ta, { key: 'z', ctrlKey: true })
    expect(value()).toBe('- a')
  })

  it('a textarea mounted later (composer expanded from collapsed) continues the list', () => {
    localStorage.setItem('mc-composer-collapsed', '1')
    renderWithProviders(<Host initial="1. test" sendOnEnter="ctrl-enter" onSend={vi.fn()} collapsible />)
    expect(screen.queryByLabelText('Message input')).toBeNull()
    act(() => { requestComposerExpand() })
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    ta.setSelectionRange(ta.value.length, ta.value.length)
    expect(newLine(ta)).toBe(true)
    expect(screen.getByTestId('value').textContent).toBe('1. test\n2. ')
  })

  it('the WebKit candidate-commit Enter (latch set, native signals clear) is not a new item', async () => {
    const { ta, value } = mount('- 項目')
    // WebKit: compositionend first, then the committing Enter keydown with
    // isComposing false and keyCode 13, inside the guard's post-composition
    // window; in ctrl-enter mode nothing claims it, so its line break follows.
    fireEvent.compositionStart(ta)
    fireEvent.compositionEnd(ta)
    expect(fireEvent.keyDown(ta, { key: 'Enter', keyCode: 13 })).toBe(true)
    expect(newLine(ta)).toBe(false)
    expect(value()).toBe('- 項目')
    // The decline is consumed by that one line break: once the latch window
    // (50ms) has passed, a plain new line continues the list again.
    await new Promise(resolve => setTimeout(resolve, 80))
    expect(fireEvent.keyDown(ta, { key: 'Enter', keyCode: 13 })).toBe(true)
    expect(newLine(ta)).toBe(true)
    expect(value()).toBe('- 項目\n- ')
  })

  it('a Gboard Return right after a composed word (keyCode 229 inside the latch window) still continues', () => {
    const { ta, value } = mount('1. 単語')
    fireEvent.compositionStart(ta)
    fireEvent.compositionEnd(ta)
    expect(fireEvent.keyDown(ta, { key: 'Unidentified', keyCode: 229 })).toBe(true)
    expect(newLine(ta)).toBe(true)
    expect(value()).toBe('1. 単語\n2. ')
  })

  it('a commit Enter claimed as the send (enter mode) does not decline a later new line', () => {
    const { ta, onSend, value } = mount('- a', 'enter')
    fireEvent.compositionStart(ta)
    fireEvent.compositionEnd(ta)
    // Latched: claimEnter consumes the key and does not send, so no beforeinput follows.
    expect(fireEvent.keyDown(ta, { key: 'Enter', keyCode: 13 })).toBe(false)
    expect(onSend).not.toHaveBeenCalled()
    // A Gboard Return's own keydown (still inside the window) resets the note
    // before its line break arrives, so that line break continues the list.
    expect(fireEvent.keyDown(ta, { key: 'Unidentified', keyCode: 229 })).toBe(true)
    expect(newLine(ta)).toBe(true)
    expect(value()).toBe('- a\n- ')
  })

  it('Ctrl+Enter in ctrl-enter mode still sends a list draft', () => {
    const { ta, onSend, value } = mount('1. test', 'ctrl-enter')
    fireEvent.keyDown(ta, { key: 'Enter', ctrlKey: true })
    expect(onSend).toHaveBeenCalledOnce()
    expect(value()).toBe('1. test')
  })
})
