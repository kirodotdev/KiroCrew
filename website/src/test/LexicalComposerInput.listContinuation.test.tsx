import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { createRef, useState } from 'react'
import type { MutableRefObject } from 'react'
import {
  COMMAND_PRIORITY_LOW,
  DELETE_CHARACTER_COMMAND,
  INSERT_LINE_BREAK_COMMAND,
  INSERT_PARAGRAPH_COMMAND,
  KEY_ENTER_COMMAND,
  UNDO_COMMAND,
  type LexicalEditor,
} from 'lexical'
import { describe, expect, it, vi } from 'vitest'
import LexicalComposerInput from '../components/LexicalComposerInput'
import type { ComposerControl } from '../components/composerControl'
import type { SendMode } from '../pages/chat/ChatSettings'
import { formatToken, type PasteBlock } from '../utils/pasteTokens'

function Host({
  initial,
  initialBlocks = [],
  sendOnEnter,
  onSend,
  editorRef,
  controlRef,
  log,
}: {
  initial: string
  initialBlocks?: PasteBlock[]
  sendOnEnter?: SendMode
  onSend: () => void
  editorRef: React.RefObject<LexicalEditor | null>
  controlRef: MutableRefObject<ComposerControl | null>
  log: string[]
}) {
  const [value, setValue] = useState(initial)
  const [blocks, setBlocks] = useState(initialBlocks)
  return (
    <>
      <LexicalComposerInput
        value={value}
        blocks={blocks}
        onChange={next => { log.push(`change:${next}`); setValue(next) }}
        onEndUndoBurst={() => log.push('endUndoBurst')}
        onBlocksChange={setBlocks}
        onSend={onSend}
        ariaLabel="Message input"
        placeholder="Write a message"
        sendOnEnter={sendOnEnter}
        editorRef={editorRef}
        controlRef={controlRef}
      />
      <output data-testid="value">{value}</output>
    </>
  )
}

async function setup(initial: string, options: { sendOnEnter?: SendMode; caret?: number; blocks?: PasteBlock[] } = {}) {
  const onSend = vi.fn()
  const editorRef = createRef<LexicalEditor>()
  const controlRef: MutableRefObject<ComposerControl | null> = { current: null }
  const log: string[] = []
  render(
    <Host
      initial={initial}
      initialBlocks={options.blocks}
      sendOnEnter={options.sendOnEnter}
      onSend={onSend}
      editorRef={editorRef}
      controlRef={controlRef}
      log={log}
    />,
  )
  await waitFor(() => expect(controlRef.current).not.toBeNull())
  act(() => controlRef.current!.setSelection(options.caret ?? initial.length))
  const editor = editorRef.current!
  const enter = (init: KeyboardEventInit = {}) => {
    act(() => {
      editor.dispatchCommand(KEY_ENTER_COMMAND, new KeyboardEvent('keydown', { key: 'Enter', cancelable: true, ...init }))
    })
  }
  const value = () => screen.getByTestId('value').textContent
  return { onSend, editor, controlRef, enter, value, log }
}

describe('LexicalComposerInput list continuation', () => {
  it('continues a bullet on Shift+Enter in the default Enter-sends mode', async () => {
    const { enter, value, onSend, controlRef } = await setup('- item')
    enter({ shiftKey: true })
    await waitFor(() => expect(value()).toBe('- item\n- '))
    expect(controlRef.current!.getSelection()).toEqual({ start: 9, end: 9 })
    expect(onSend).not.toHaveBeenCalled()
  })

  it('sends on Enter without touching the list', async () => {
    const { enter, value, onSend } = await setup('- item')
    enter()
    expect(onSend).toHaveBeenCalledTimes(1)
    expect(value()).toBe('- item')
  })

  it('continues on plain Enter in ctrl-enter mode, and Ctrl+Enter still sends', async () => {
    const { enter, value, onSend } = await setup('3. step', { sendOnEnter: 'ctrl-enter' })
    enter()
    await waitFor(() => expect(value()).toBe('3. step\n4. '))
    expect(onSend).not.toHaveBeenCalled()
    enter({ ctrlKey: true })
    expect(onSend).toHaveBeenCalledTimes(1)
    expect(value()).toBe('3. step\n4. ')
  })

  it('continues on Ctrl+Enter in enter-ctrl-newline mode', async () => {
    const { enter, value, onSend } = await setup('- [x] done', { sendOnEnter: 'enter-ctrl-newline' })
    enter({ ctrlKey: true })
    await waitFor(() => expect(value()).toBe('- [x] done\n- [ ] '))
    expect(onSend).not.toHaveBeenCalled()
  })

  it('ends the list when the new line lands on an empty item', async () => {
    const { enter, value, controlRef } = await setup('- one\n- ')
    enter({ shiftKey: true })
    await waitFor(() => expect(value()).toBe('- one\n'))
    expect(controlRef.current!.getSelection()).toEqual({ start: 6, end: 6 })
  })

  it('splits an item at a mid-line caret', async () => {
    const { enter, value } = await setup('1. alpha beta', { caret: 9 })
    enter({ shiftKey: true })
    await waitFor(() => expect(value()).toBe('1. alpha \n2. beta'))
  })

  it('inserts a plain newline off a list line', async () => {
    const plain = await setup('hello')
    plain.enter({ shiftKey: true })
    await waitFor(() => expect(plain.value()).toBe('hello\n'))
  })

  it('inserts a plain newline with the caret inside the marker', async () => {
    const { enter, value } = await setup('- item', { caret: 1 })
    enter({ shiftKey: true })
    await waitFor(() => expect(value()).toBe('-\n item'))
  })

  it('continues after a trailing paste chip', async () => {
    const block: PasteBlock = { id: 'paste-1', seq: 1, lines: 4, content: 'a\nb\nc\nd' }
    const initial = `- ${formatToken(block)}`
    const { enter, value } = await setup(initial, { blocks: [block] })
    enter({ shiftKey: true })
    await waitFor(() => expect(value()).toBe(`${initial}\n- `))
  })

  it('renumbers the items below a new one and keeps the caret on it', async () => {
    const { enter, value, controlRef } = await setup('1. foo\n2. bar\n3. baz', { caret: 3 })
    enter({ shiftKey: true })
    await waitFor(() => expect(value()).toBe('1. \n2. foo\n3. bar\n4. baz'))
    expect(controlRef.current!.getSelection()).toEqual({ start: 7, end: 7 })
  })

  it('undoes a renumbering continuation in one step', async () => {
    const { enter, value, editor } = await setup('1. foo\n2. bar', { caret: 6 })
    enter({ shiftKey: true })
    await waitFor(() => expect(value()).toBe('1. foo\n2. \n3. bar'))
    act(() => { editor.dispatchCommand(UNDO_COMMAND, undefined) })
    await waitFor(() => expect(value()).toBe('1. foo\n2. bar'))
  })

  it('renumbers past a paste chip on a later item', async () => {
    const block: PasteBlock = { id: 'paste-1', seq: 1, lines: 4, content: 'a\nb\nc\nd' }
    const initial = `1. a\n2. ${formatToken(block)}\n3. c`
    const { enter, value } = await setup(initial, { blocks: [block], caret: 4 })
    enter({ shiftKey: true })
    await waitFor(() => expect(value()).toBe(`1. a\n2. \n3. ${formatToken(block)}\n4. c`))
  })

  it('undoes the continuation in one step', async () => {
    const { enter, value, editor } = await setup('- item')
    enter({ shiftKey: true })
    await waitFor(() => expect(value()).toBe('- item\n- '))
    act(() => { editor.dispatchCommand(UNDO_COMMAND, undefined) })
    await waitFor(() => expect(value()).toBe('- item'))
  })

  it('never continues on an IME composition Enter', async () => {
    const { enter, value, onSend, editor } = await setup('- item', { sendOnEnter: 'ctrl-enter' })
    const root = editor.getRootElement()!
    fireEvent.compositionStart(root)
    enter()
    // A composing Enter is claimed before any line-break command runs, and a
    // line break that arrives through beforeinput while the latch is held
    // stays an ordinary break.
    expect(value()).toBe('- item')
    act(() => { editor.dispatchCommand(INSERT_LINE_BREAK_COMMAND, false) })
    await waitFor(() => expect(value()).toBe('- item\n'))
    expect(onSend).not.toHaveBeenCalled()
  })

  it('continues on the paragraph command WebKit beforeinput dispatches', async () => {
    const { value, editor } = await setup('* item')
    act(() => { editor.dispatchCommand(INSERT_PARAGRAPH_COMMAND, undefined) })
    await waitFor(() => expect(value()).toBe('* item\n* '))
  })

  it('leaves the caret-stays-put line break alone', async () => {
    const { value, editor } = await setup('- item')
    act(() => { editor.dispatchCommand(INSERT_LINE_BREAK_COMMAND, true) })
    await waitFor(() => expect(value()).toBe('- item\n'))
  })
})

describe('LexicalComposerInput list marker Backspace', () => {
  const backspace = (editor: LexicalEditor) => {
    act(() => { editor.dispatchCommand(DELETE_CHARACTER_COMMAND, true) })
  }

  it('clears the number a continuation just made in one press', async () => {
    const { enter, value, editor, controlRef } = await setup('1. test', { sendOnEnter: 'ctrl-enter' })
    enter()
    await waitFor(() => expect(value()).toBe('1. test\n2. '))
    backspace(editor)
    await waitFor(() => expect(value()).toBe('1. test\n'))
    expect(controlRef.current!.getSelection()).toEqual({ start: 8, end: 8 })
  })

  it('moves the items below back up when it clears a mid-list item, in one undo step', async () => {
    const { enter, value, editor, controlRef } = await setup('1. foo\n2. bar', { caret: 6 })
    enter({ shiftKey: true })
    await waitFor(() => expect(value()).toBe('1. foo\n2. \n3. bar'))
    backspace(editor)
    await waitFor(() => expect(value()).toBe('1. foo\n\n2. bar'))
    expect(controlRef.current!.getSelection()).toEqual({ start: 7, end: 7 })
    act(() => { editor.dispatchCommand(UNDO_COMMAND, undefined) })
    await waitFor(() => expect(value()).toBe('1. foo\n2. \n3. bar'))
  })

  it('removes only the marker in front of item text', async () => {
    const { value, editor } = await setup('- [ ] todo', { caret: 6 })
    backspace(editor)
    await waitFor(() => expect(value()).toBe('todo'))
  })

  // jsdom cannot run Lexical's own character delete (it needs the DOM
  // selection's `modify`), so a pass-through is proven by the command reaching
  // a lower-priority handler with the text untouched.
  const passThrough = (editor: LexicalEditor) => {
    const reached = vi.fn(() => true)
    const unregister = editor.registerCommand(DELETE_CHARACTER_COMMAND, reached, COMMAND_PRIORITY_LOW)
    return { reached, unregister }
  }

  it('ends the host typing burst before the marker removal reaches onChange', async () => {
    const { value, editor, log } = await setup('- ab', { caret: 2 })
    log.length = 0
    backspace(editor)
    await waitFor(() => expect(value()).toBe('ab'))
    expect(log).toEqual(['endUndoBurst', 'change:ab'])
  })

  it('leaves a Backspace away from the marker boundary to the editor', async () => {
    const { value, editor, log } = await setup('- item')
    log.length = 0
    const { reached, unregister } = passThrough(editor)
    backspace(editor)
    expect(reached).toHaveBeenCalledWith(true, editor)
    expect(value()).toBe('- item')
    expect(log).toEqual([])
    unregister()
  })

  it('leaves a forward Delete to the editor', async () => {
    const { value, editor } = await setup('- a', { caret: 2 })
    const { reached, unregister } = passThrough(editor)
    act(() => { editor.dispatchCommand(DELETE_CHARACTER_COMMAND, false) })
    expect(reached).toHaveBeenCalledWith(false, editor)
    expect(value()).toBe('- a')
    unregister()
  })

  it('undoes the marker removal in one step', async () => {
    const { value, editor } = await setup('- a\n- ')
    backspace(editor)
    await waitFor(() => expect(value()).toBe('- a\n'))
    act(() => { editor.dispatchCommand(UNDO_COMMAND, undefined) })
    await waitFor(() => expect(value()).toBe('- a\n- '))
  })
})
