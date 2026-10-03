import { act, render, screen, waitFor } from '@testing-library/react'
import { createRef, useState } from 'react'
import type { MutableRefObject, RefObject } from 'react'
import { CAN_UNDO_COMMAND, COMMAND_PRIORITY_LOW, CONTROLLED_TEXT_INSERTION_COMMAND, KEY_TAB_COMMAND, UNDO_COMMAND, type LexicalEditor } from 'lexical'
import { describe, expect, it, vi } from 'vitest'
import LexicalComposerInput from '../components/LexicalComposerInput'
import type { ComposerControl } from '../components/composerControl'
import { formatToken, type PasteBlock } from '../utils/pasteTokens'

/* ── Tab / Shift+Tab list indent in the Lexical composer. ── */

function Host({
  initial,
  initialBlocks = [],
  editorRef,
  controlRef,
  isMenuOpen,
}: {
  initial: string
  initialBlocks?: PasteBlock[]
  editorRef: RefObject<LexicalEditor | null>
  controlRef: MutableRefObject<ComposerControl | null>
  isMenuOpen?: () => boolean
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
        onSend={vi.fn()}
        ariaLabel="Message input"
        placeholder="Write a message"
        editorRef={editorRef}
        controlRef={controlRef}
        isMenuOpen={isMenuOpen}
      />
      <output data-testid="value">{value}</output>
    </>
  )
}

async function mount(initial: string, caret: number, extra: { isMenuOpen?: () => boolean; blocks?: PasteBlock[] } = {}) {
  const editorRef = createRef<LexicalEditor>()
  const controlRef: MutableRefObject<ComposerControl | null> = { current: null }
  render(<Host initial={initial} initialBlocks={extra.blocks} editorRef={editorRef} controlRef={controlRef} isMenuOpen={extra.isMenuOpen} />)
  await waitFor(() => expect(controlRef.current).not.toBeNull())
  act(() => controlRef.current!.setSelection(caret))
  return { editor: editorRef.current!, control: controlRef.current! }
}

/** Dispatch Tab; returns [claimed, defaultPrevented]. */
function tab(editor: LexicalEditor, init: KeyboardEventInit = {}, extra: Record<string, unknown> = {}) {
  const event = new KeyboardEvent('keydown', { key: 'Tab', cancelable: true, ...init })
  for (const [key, value] of Object.entries(extra)) Object.defineProperty(event, key, { value })
  let claimed = false
  act(() => { claimed = editor.dispatchCommand(KEY_TAB_COMMAND, event) })
  return [claimed, event.defaultPrevented]
}

const value = () => screen.getByTestId('value').textContent

describe('LexicalComposerInput: Tab indents list lines', () => {
  it('Tab on a list line adds two spaces and keeps the caret on its character', async () => {
    const { editor, control } = await mount('intro\n- item', 'intro\n- ite'.length)
    expect(tab(editor)).toEqual([true, true])
    await waitFor(() => expect(value()).toBe('intro\n  - item'))
    expect(control.getSelection()).toEqual({ start: 'intro\n  - ite'.length, end: 'intro\n  - ite'.length })
  })

  it('Shift+Tab on an indented list line removes one level', async () => {
    const { editor, control } = await mount('    1. deep', 9)
    expect(tab(editor, { shiftKey: true })).toEqual([true, true])
    await waitFor(() => expect(value()).toBe('  1. deep'))
    expect(control.getSelection()).toEqual({ start: 7, end: 7 })
  })

  it('indents a list line that follows a paste chip', async () => {
    const block: PasteBlock = { id: 'p1', seq: 1, lines: 4, content: 'a\nb\nc\nd' }
    const token = formatToken(block)
    const initial = `${token}\n- item`
    const { editor } = await mount(initial, initial.length, { blocks: [block] })
    expect(tab(editor)).toEqual([true, true])
    await waitFor(() => expect(value()).toBe(`${token}\n  - item`))
  })

  it('Tab on a focused paste chip is not claimed and leaves the draft unchanged', async () => {
    const block: PasteBlock = { id: 'p1', seq: 1, lines: 4, content: 'a\nb\nc\nd' }
    const token = formatToken(block)
    const initial = `${token}\n- item`
    const { editor } = await mount(initial, initial.length, { blocks: [block] })
    const chip = screen.getByTestId('paste-token-1')

    expect(tab(editor, {}, { target: chip })).toEqual([false, false])
    expect(value()).toBe(initial)
  })

  it('Tab on a plain line is not claimed so focus moves', async () => {
    const { editor } = await mount('plain text', 3)
    expect(tab(editor)).toEqual([false, false])
    expect(value()).toBe('plain text')
  })

  it('Shift+Tab on an unindented list line is not claimed', async () => {
    const { editor } = await mount('- top', 3)
    expect(tab(editor, { shiftKey: true })).toEqual([false, false])
    expect(value()).toBe('- top')
  })

  it('a ranged selection is not claimed', async () => {
    const { editor, control } = await mount('- item', 6)
    act(() => control.setSelection(2, 6))
    expect(tab(editor)).toEqual([false, false])
  })

  it('Tab during IME composition is left to the IME', async () => {
    const { editor } = await mount('- item', 3)
    // Claimed so no other handler acts on it, but not prevented: the IME owns it.
    expect(tab(editor, {}, { isComposing: true, keyCode: 229 })).toEqual([true, false])
    expect(value()).toBe('- item')
  })

  it('the WebKit commit Tab after compositionend does not indent or move focus', async () => {
    const { editor } = await mount('- item', 3)
    const root = screen.getByRole('textbox')
    act(() => {
      root.dispatchEvent(new CompositionEvent('compositionstart'))
      root.dispatchEvent(new CompositionEvent('compositionend'))
    })
    // isComposing is already false on this keydown; only the latch knows.
    expect(tab(editor)).toEqual([true, true])
    expect(value()).toBe('- item')
  })

  it('Tab while a suggestion menu is open is not claimed', async () => {
    const { editor } = await mount('- item', 3, { isMenuOpen: () => true })
    expect(tab(editor)).toEqual([false, false])
    expect(value()).toBe('- item')
  })

  it('one undo reverts one indent step', async () => {
    const { editor } = await mount('- item', 3)
    let canUndo = false
    const unregister = editor.registerCommand(CAN_UNDO_COMMAND, next => { canUndo = next; return false }, COMMAND_PRIORITY_LOW)
    // Two separate keystrokes (Lexical batches updates dispatched in one task).
    tab(editor)
    await waitFor(() => expect(value()).toBe('  - item'))
    tab(editor)
    await waitFor(() => expect(value()).toBe('    - item'))
    await waitFor(() => expect(canUndo).toBe(true))
    act(() => { editor.dispatchCommand(UNDO_COMMAND, undefined) })
    await waitFor(() => expect(value()).toBe('  - item'))
    unregister()
  })

  it('undo right after typing reverts the indent, not the typing', async () => {
    const { editor } = await mount('- ite', 5)
    act(() => { editor.dispatchCommand(CONTROLLED_TEXT_INSERTION_COMMAND, 'm') })
    await waitFor(() => expect(value()).toBe('- item'))
    tab(editor)
    await waitFor(() => expect(value()).toBe('  - item'))
    act(() => { editor.dispatchCommand(UNDO_COMMAND, undefined) })
    await waitFor(() => expect(value()).toBe('- item'))
  })
})
