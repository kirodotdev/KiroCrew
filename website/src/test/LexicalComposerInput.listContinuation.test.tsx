import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { createRef, useState } from 'react'
import type { MutableRefObject } from 'react'
import {
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
}: {
  initial: string
  initialBlocks?: PasteBlock[]
  sendOnEnter?: SendMode
  onSend: () => void
  editorRef: React.RefObject<LexicalEditor | null>
  controlRef: MutableRefObject<ComposerControl | null>
}) {
  const [value, setValue] = useState(initial)
  const [blocks, setBlocks] = useState(initialBlocks)
  return (
    <>
      <LexicalComposerInput
        value={value}
        blocks={blocks}
        onChange={setValue}
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
  render(
    <Host
      initial={initial}
      initialBlocks={options.blocks}
      sendOnEnter={options.sendOnEnter}
      onSend={onSend}
      editorRef={editorRef}
      controlRef={controlRef}
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
  return { onSend, editor, controlRef, enter, value }
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
