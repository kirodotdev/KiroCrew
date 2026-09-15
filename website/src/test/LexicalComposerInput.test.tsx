import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createRef, useEffect, useState } from 'react'
import type { MutableRefObject, RefObject } from 'react'
import { $getRoot, $getSelection, $isRangeSelection, CAN_REDO_COMMAND, CAN_UNDO_COMMAND, COMMAND_PRIORITY_LOW, CONTROLLED_TEXT_INSERTION_COMMAND, COPY_COMMAND, KEY_ARROW_DOWN_COMMAND, KEY_ARROW_UP_COMMAND, KEY_ENTER_COMMAND, KEY_MODIFIER_COMMAND, PASTE_COMMAND, REDO_COMMAND, UNDO_COMMAND, type LexicalCommand, type LexicalEditor } from 'lexical'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import LexicalComposerInput from '../components/LexicalComposerInput'
import type { ComposerControl } from '../components/composerControl'
import { expandAll, formatToken, type PasteBlock } from '../utils/pasteTokens'

const block: PasteBlock = {
  id: 'paste-1',
  seq: 1,
  lines: 4,
  content: 'alpha\nbeta\ngamma\ndelta',
}

async function dispatchAtEnd<T>(editor: LexicalEditor, command: LexicalCommand<T>, payload: T) {
  act(() => { editor.update(() => $getRoot().selectEnd(), { discrete: true }) })
  await new Promise<void>(resolve => setTimeout(resolve, 0))
  act(() => { editor.dispatchCommand(command, payload) })
}

function ControlledHost({
  initial = '',
  initialBlocks = [] as PasteBlock[],
  onSend = vi.fn(),
  editorRef,
  controlRef,
  onUploadFiles,
  onSelectionChange,
  sentMessages,
  showFullPastes,
  onReady,
  historyKey,
  restoreValue,
  inlineOnChange = false,
}: {
  initial?: string
  initialBlocks?: PasteBlock[]
  onSend?: () => void
  editorRef?: RefObject<LexicalEditor | null>
  controlRef?: MutableRefObject<ComposerControl | null>
  onUploadFiles?: (files: File[]) => void
  onSelectionChange?: (selection: { start: number; end: number }) => void
  sentMessages?: string[]
  showFullPastes?: boolean
  onReady?: () => void
  historyKey?: string | null
  restoreValue?: string
  /** Pass a fresh onChange identity on every render, as ChatPage does. */
  inlineOnChange?: boolean
}) {
  const [value, setValue] = useState(initial)
  const [blocks, setBlocks] = useState(initialBlocks)
  useEffect(() => {
    if (restoreValue !== undefined) setValue(restoreValue)
  }, [restoreValue])
  return (
    <>
      <LexicalComposerInput
        value={value}
        blocks={blocks}
        onChange={inlineOnChange ? (next: string) => setValue(next) : setValue}
        onBlocksChange={setBlocks}
        showFullPastes={showFullPastes}
        onSend={onSend}
        ariaLabel="Message input"
        placeholder="Write a message"
        editorRef={editorRef}
        controlRef={controlRef}
        onUploadFiles={onUploadFiles}
        onSelectionChange={onSelectionChange}
        sentMessages={sentMessages}
        onReady={onReady}
        historyKey={historyKey}
      />
      <output data-testid="value">{value}</output>
      <output data-testid="blocks">{JSON.stringify(blocks)}</output>
    </>
  )
}

describe('LexicalComposerInput', () => {
  beforeEach(() => {
    vi.useRealTimers()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('hydrates canonical markers as true inline non-editable chips', () => {
    render(<ControlledHost initial={`before ${formatToken(block)} after`} initialBlocks={[block]} />)
    const chip = screen.getByTestId('paste-token-1')
    expect(chip).toHaveTextContent(/4 lines/) // first-line snippet + count
    expect(chip).not.toHaveTextContent('[ Paste')
    expect(chip.closest('[contenteditable="false"]')).not.toBeNull()
    expect(screen.getByRole('textbox')).toHaveAttribute('data-lexical-composer')
  })

  it('reports ordinary typing through the controlled value contract', async () => {
    const editorRef = createRef<LexicalEditor>()
    render(<ControlledHost editorRef={editorRef} />)
    await waitFor(() => expect(editorRef.current).not.toBeNull())
    await dispatchAtEnd(editorRef.current!, CONTROLLED_TEXT_INSERTION_COMMAND, 'hello')
    await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent('hello'))
  })

  it('collapses a large paste into the canonical marker and PasteBlock sidecar', async () => {
    const editorRef = createRef<LexicalEditor>()
    render(<ControlledHost editorRef={editorRef} />)
    await waitFor(() => expect(editorRef.current).not.toBeNull())
    const payload = 'one\ntwo\nthree\nfour'
    const event = new Event('paste', { cancelable: true }) as ClipboardEvent
    Object.defineProperty(event, 'clipboardData', {
      value: { getData: (type: string) => type === 'text/plain' ? payload : '', types: ['text/plain'] },
    })
    await dispatchAtEnd(editorRef.current!, PASTE_COMMAND, event)
    await waitFor(() => expect(screen.getByTestId('value').textContent).toMatch(/\[ Paste #1 · 4 lines \]/))
    expect(screen.getByTestId('blocks').textContent).toContain(payload.replaceAll('\n', '\\n'))
    expect(screen.getByTestId('paste-token-1')).toBeInTheDocument()
  })

  it('anchors the preview to the real Lexical node element', async () => {
    render(<ControlledHost initial={formatToken(block)} initialBlocks={[block]} />)
    const chip = screen.getByTestId('paste-token-1')
    const rect = { left: 42, top: 18, right: 142, bottom: 38, width: 100, height: 20, x: 42, y: 18, toJSON: () => ({}) }
    vi.spyOn(chip, 'getBoundingClientRect').mockReturnValue(rect as DOMRect)
    fireEvent.click(chip)
    // Anchored to the chip: same left; no room above (top 18) so it opens BELOW.
    const preview = await screen.findByTestId('paste-preview-editor')
    expect(preview).toHaveStyle({ left: '42px', top: '44px' })
  })

  it('applies parent-driven controlled value and sidecar updates', async () => {
    const { rerender } = render(
      <LexicalComposerInput
        value="plain"
        blocks={[]}
        onChange={vi.fn()}
        onBlocksChange={vi.fn()}
        onSend={vi.fn()}
        ariaLabel="Message input"
        placeholder="Write a message"
      />,
    )
    rerender(
      <LexicalComposerInput
        value={formatToken(block)}
        blocks={[block]}
        onChange={vi.fn()}
        onBlocksChange={vi.fn()}
        onSend={vi.fn()}
        ariaLabel="Message input"
        placeholder="Write a message"
      />,
    )
    expect(await screen.findByTestId('paste-token-1')).toBeInTheDocument()
  })

  it('copies and cuts a selection spanning a paste chip as expanded text', async () => {
    const user = userEvent.setup()
    render(<ControlledHost initial={formatToken(block)} initialBlocks={[block]} />)
    const chip = screen.getByTestId('paste-token-1')
    // Clicking a pill opens its editable preview; Escape closes it and parks
    // the caret right after the pill. Select-all then spans the pill.
    await user.click(chip)
    await user.keyboard('{Escape}')
    const editor = screen.getByRole('textbox')
    await user.keyboard('{Control>}a{/Control}')
    const clipboard = { setData: vi.fn() }
    fireEvent.copy(editor, { clipboardData: clipboard })
    expect(clipboard.setData).toHaveBeenCalledWith('text/plain', block.content)
    fireEvent.cut(editor, { clipboardData: clipboard })
    await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent(''))
    expect(screen.getByTestId('blocks')).toHaveTextContent('[]')
  })

  it('deletes a paste chip atomically with one Backspace', async () => {
    const user = userEvent.setup()
    render(<ControlledHost initial={`x${formatToken(block)}y`} initialBlocks={[block]} />)
    // Click opens the preview; Escape closes it with the caret right after the pill.
    await user.click(screen.getByTestId('paste-token-1'))
    await user.keyboard('{Escape}')
    await user.keyboard('{Backspace}')
    await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent('xy'))
    expect(screen.queryByTestId('paste-token-1')).not.toBeInTheDocument()
  })

  it('clears undo history when an equal-valued draft moves to another slot', async () => {
    const editorRef = createRef<LexicalEditor>()
    const onChange = vi.fn()
    const props = {
      value: '',
      blocks: [] as PasteBlock[],
      onChange,
      onSend: vi.fn(),
      ariaLabel: 'Message input',
      placeholder: 'Write a message',
      editorRef,
    }
    const { rerender } = render(<LexicalComposerInput {...props} historyKey="slot-a" />)
    await waitFor(() => expect(editorRef.current).not.toBeNull())
    let canUndo = false
    const unregisterUndo = editorRef.current!.registerCommand(
      CAN_UNDO_COMMAND,
      value => { canUndo = value; return false },
      COMMAND_PRIORITY_LOW,
    )

    await dispatchAtEnd(editorRef.current!, CONTROLLED_TEXT_INSERTION_COMMAND, 'hello')
    await waitFor(() => expect(onChange).toHaveBeenLastCalledWith('hello'))
    await waitFor(() => expect(canUndo).toBe(true))

    rerender(<LexicalComposerInput {...props} value="hello" historyKey="slot-b" />)
    await waitFor(() => expect(canUndo).toBe(false))
    act(() => { editorRef.current!.dispatchCommand(UNDO_COMMAND, undefined) })
    expect(editorRef.current!.getEditorState().read(() => $getRoot().getTextContent())).toBe('hello')
    expect(onChange).not.toHaveBeenCalledWith('')
    unregisterUndo()
  })

  it('supports undo and redo through Lexical history', async () => {
    const editorRef = createRef<LexicalEditor>()
    render(<ControlledHost editorRef={editorRef} />)
    await waitFor(() => expect(editorRef.current).not.toBeNull())
    let canUndo = false
    let canRedo = false
    const unregisterUndo = editorRef.current!.registerCommand(CAN_UNDO_COMMAND, value => { canUndo = value; return false }, COMMAND_PRIORITY_LOW)
    const unregisterRedo = editorRef.current!.registerCommand(CAN_REDO_COMMAND, value => { canRedo = value; return false }, COMMAND_PRIORITY_LOW)
    await dispatchAtEnd(editorRef.current!, CONTROLLED_TEXT_INSERTION_COMMAND, 'hello')
    await waitFor(() => expect(canUndo).toBe(true))
    await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent('hello'))
    act(() => { editorRef.current!.dispatchCommand(UNDO_COMMAND, undefined) })
    await waitFor(() => expect(screen.getByTestId('value').textContent).toBe(''))
    await waitFor(() => expect(canRedo).toBe(true))
    act(() => { editorRef.current!.dispatchCommand(REDO_COMMAND, undefined) })
    await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent('hello'))
    unregisterUndo()
    unregisterRedo()
  })

  it('copies a mixed partial range without mutating editor content or history', async () => {
    const editorRef = createRef<LexicalEditor>()
    const initial = `before ${formatToken(block)} after`
    render(<ControlledHost initial={initial} initialBlocks={[block]} editorRef={editorRef} />)
    await waitFor(() => expect(editorRef.current).not.toBeNull())
    let canUndo = false
    const unregisterUndo = editorRef.current!.registerCommand(
      CAN_UNDO_COMMAND,
      value => { canUndo = value; return false },
      COMMAND_PRIORITY_LOW,
    )
    act(() => {
      editorRef.current!.update(() => {
        const paragraph = $getRoot().getFirstChildOrThrow()
        const first = paragraph.getFirstChildOrThrow()
        const last = paragraph.getLastChildOrThrow()
        first.select(2, 2)
        const selection = $getSelection()
        if (!$isRangeSelection(selection)) throw new Error('range selection expected')
        selection.focus.set(last.getKey(), 3, 'text')
      }, { discrete: true })
    })
    const clipboard = { setData: vi.fn() }
    const event = new Event('copy', { cancelable: true }) as ClipboardEvent
    Object.defineProperty(event, 'clipboardData', { value: clipboard })
    act(() => { editorRef.current!.dispatchCommand(COPY_COMMAND, event) })
    expect(clipboard.setData).toHaveBeenCalledWith('text/plain', `fore ${block.content} af`)
    expect(screen.getByTestId('value')).toHaveTextContent(initial)
    expect(canUndo).toBe(false)
    unregisterUndo()
  })

  it('keeps a programmatic replacement separate from the preceding typing burst', async () => {
    const editorRef = createRef<LexicalEditor>()
    const controlRef: MutableRefObject<ComposerControl | null> = { current: null }
    const initial = `draft ${formatToken(block)}`
    render(
      <ControlledHost
        initial={initial}
        initialBlocks={[block]}
        editorRef={editorRef}
        controlRef={controlRef}
      />,
    )
    await waitFor(() => expect(controlRef.current).not.toBeNull())

    await dispatchAtEnd(editorRef.current!, CONTROLLED_TEXT_INSERTION_COMMAND, ' typed')
    const typed = `${initial} typed`
    await waitFor(() => expect(screen.getByTestId('value').textContent).toBe(typed))

    act(() => controlRef.current?.replaceText?.(`optimized ${formatToken(block)}`))
    await waitFor(() => expect(screen.getByTestId('value').textContent).toBe(`optimized ${formatToken(block)}`))

    act(() => { editorRef.current!.dispatchCommand(UNDO_COMMAND, undefined) })
    await waitFor(() => expect(screen.getByTestId('value').textContent).toBe(typed))
    expect(JSON.parse(screen.getByTestId('blocks').textContent!)).toEqual([block])
  })

  it('restores a restored draft when a programmatic replacement is undone without prior typing', async () => {
    const editorRef = createRef<LexicalEditor>()
    const controlRef: MutableRefObject<ComposerControl | null> = { current: null }
    render(<ControlledHost initial="draft" editorRef={editorRef} controlRef={controlRef} />)
    await waitFor(() => expect(controlRef.current).not.toBeNull())

    let canUndo = false
    const unregisterUndo = editorRef.current!.registerCommand(
      CAN_UNDO_COMMAND,
      value => { canUndo = value; return false },
      COMMAND_PRIORITY_LOW,
    )

    act(() => controlRef.current?.replaceText?.('optimized'))
    await waitFor(() => expect(screen.getByTestId('value').textContent).toBe('optimized'))
    await waitFor(() => expect(canUndo).toBe(true))

    act(() => { editorRef.current!.dispatchCommand(UNDO_COMMAND, undefined) })
    await waitFor(() => expect(screen.getByTestId('value').textContent).toBe('draft'))
    unregisterUndo()
  })

  it('records a mounted host value change as one undo step', async () => {
    const editorRef = createRef<LexicalEditor>()
    const props = { editorRef }
    const { rerender } = render(<ControlledHost {...props} />)
    await waitFor(() => expect(editorRef.current).not.toBeNull())
    let canUndo = false
    const unregisterUndo = editorRef.current!.registerCommand(
      CAN_UNDO_COMMAND,
      value => { canUndo = value; return false },
      COMMAND_PRIORITY_LOW,
    )

    await dispatchAtEnd(editorRef.current!, CONTROLLED_TEXT_INSERTION_COMMAND, 'hello')
    await waitFor(() => expect(screen.getByTestId('value').textContent).toBe('hello'))
    rerender(<ControlledHost {...props} restoreValue="/help " />)
    await waitFor(() => expect(screen.getByTestId('value').textContent).toBe('/help '))
    await waitFor(() => expect(canUndo).toBe(true))

    act(() => { editorRef.current!.dispatchCommand(UNDO_COMMAND, undefined) })
    await waitFor(() => expect(screen.getByTestId('value').textContent).toBe('hello'))
    expect(canUndo).toBe(true)
    unregisterUndo()
  })

  it('does not record the first host value change after a history key change', async () => {
    const editorRef = createRef<LexicalEditor>()
    const props = { initial: 'slot-a draft', editorRef }
    const { rerender } = render(<ControlledHost {...props} historyKey="slot-a" />)
    await waitFor(() => expect(editorRef.current).not.toBeNull())
    let canUndo = false
    const unregisterUndo = editorRef.current!.registerCommand(
      CAN_UNDO_COMMAND,
      value => { canUndo = value; return false },
      COMMAND_PRIORITY_LOW,
    )

    rerender(<ControlledHost {...props} historyKey="slot-b" />)
    rerender(<ControlledHost {...props} historyKey="slot-b" restoreValue="other draft" />)
    await waitFor(() => expect(screen.getByTestId('value').textContent).toBe('other draft'))
    await waitFor(() => expect(canUndo).toBe(false))

    act(() => { editorRef.current!.dispatchCommand(UNDO_COMMAND, undefined) })
    expect(editorRef.current!.getEditorState().read(() => $getRoot().getTextContent())).toBe('other draft')
    expect(canUndo).toBe(false)
    unregisterUndo()
  })

  it('keeps the slot restore non-undoable across an unrelated re-render between the key change and the restore', async () => {
    // ChatPage passes an inline onChange and re-renders per streamed chunk, so
    // the controlled sync's effect can re-run with nothing changed between the
    // history-key commit and the host's separate draft-restore commit. That
    // re-run must not consume the settle flag: if it did, the restore would be
    // pushed as an undo step and Ctrl+Z would resurrect the previous slot's
    // draft in the new slot.
    const editorRef = createRef<LexicalEditor>()
    const props = { initial: 'slot-a draft', editorRef, inlineOnChange: true }
    const { rerender } = render(<ControlledHost {...props} historyKey="slot-a" />)
    await waitFor(() => expect(editorRef.current).not.toBeNull())
    let canUndo = false
    const unregisterUndo = editorRef.current!.registerCommand(
      CAN_UNDO_COMMAND,
      value => { canUndo = value; return false },
      COMMAND_PRIORITY_LOW,
    )

    rerender(<ControlledHost {...props} historyKey="slot-b" />)
    // The interleaved render: same value, same key, new onChange identity.
    rerender(<ControlledHost {...props} historyKey="slot-b" />)
    rerender(<ControlledHost {...props} historyKey="slot-b" restoreValue="other draft" />)
    await waitFor(() => expect(screen.getByTestId('value').textContent).toBe('other draft'))
    await waitFor(() => expect(canUndo).toBe(false))

    act(() => { editorRef.current!.dispatchCommand(UNDO_COMMAND, undefined) })
    await new Promise(resolve => setTimeout(resolve, 20))
    expect(screen.getByTestId('value').textContent).toBe('other draft')
    unregisterUndo()
  })

  it('does not send when Enter commits an IME composition', async () => {
    const onSend = vi.fn()
    const editorRef = createRef<LexicalEditor>()
    render(<ControlledHost initial="draft" onSend={onSend} editorRef={editorRef} />)
    await waitFor(() => expect(editorRef.current).not.toBeNull())
    const event = new KeyboardEvent('keydown', { key: 'Enter' })
    Object.defineProperties(event, { isComposing: { value: true }, keyCode: { value: 229 } })
    act(() => { editorRef.current!.dispatchCommand(KEY_ENTER_COMMAND, event) })
    expect(onSend).not.toHaveBeenCalled()
    expect(screen.getByTestId('value')).toHaveTextContent('draft')
  })

  it('does not send on the WebKit commit Enter that lands after compositionend', async () => {
    // On WebKit the keydown that COMMITS a candidate arrives AFTER
    // `compositionend` with `isComposing` already false, so the native flags
    // alone cannot identify it — only the shared post-composition latch can.
    vi.useFakeTimers()
    try {
      const onSend = vi.fn()
      const editorRef = createRef<LexicalEditor>()
      render(<ControlledHost initial="draft" onSend={onSend} editorRef={editorRef} />)
      expect(editorRef.current).not.toBeNull()
      const root = editorRef.current!.getRootElement()!
      fireEvent.compositionStart(root)
      fireEvent.compositionEnd(root)
      const commitEnter = new KeyboardEvent('keydown', { key: 'Enter', cancelable: true })
      act(() => { editorRef.current!.dispatchCommand(KEY_ENTER_COMMAND, commitEnter) })
      expect(onSend).not.toHaveBeenCalled()
      // Past the post-composition window the same keystroke is a real send.
      act(() => { vi.advanceTimersByTime(60) })
      const plainEnter = new KeyboardEvent('keydown', { key: 'Enter', cancelable: true })
      act(() => { editorRef.current!.dispatchCommand(KEY_ENTER_COMMAND, plainEnter) })
      expect(onSend).toHaveBeenCalledTimes(1)
    } finally {
      vi.useRealTimers()
    }
  })

  it('exposes canonical selection offsets across paste-token nodes', async () => {
    const controlRef: MutableRefObject<ComposerControl | null> = { current: null }
    const onSelectionChange = vi.fn()
    const initial = `before ${formatToken(block)} after`
    render(
      <ControlledHost
        initial={initial}
        initialBlocks={[block]}
        controlRef={controlRef}
        onSelectionChange={onSelectionChange}
      />,
    )
    await waitFor(() => expect(controlRef.current).not.toBeNull())
    act(() => controlRef.current!.setSelection(2, initial.length - 2, { focus: true }))
    expect(controlRef.current!.getSelection()).toEqual({ start: 2, end: initial.length - 2 })
    await waitFor(() => expect(onSelectionChange).toHaveBeenCalledWith({
      start: 2,
      end: initial.length - 2,
    }))
    expect(screen.getByRole('textbox')).toHaveFocus()
  })

  it('prefers text/plain over incidental clipboard images and never imports rich HTML', async () => {
    const editorRef = createRef<LexicalEditor>()
    const onUploadFiles = vi.fn()
    render(<ControlledHost editorRef={editorRef} onUploadFiles={onUploadFiles} />)
    await waitFor(() => expect(editorRef.current).not.toBeNull())
    const image = new File(['px'], 'image.png', { type: 'image/png' })
    const event = new Event('paste', { cancelable: true }) as ClipboardEvent
    Object.defineProperty(event, 'clipboardData', {
      value: {
        types: ['text/plain', 'text/html', 'Files'],
        items: [{ kind: 'file', type: 'image/png', getAsFile: () => image }],
        getData: (type: string) => type === 'text/plain' ? 'plain text' : '<b>rich</b>',
      },
    })
    await dispatchAtEnd(editorRef.current!, PASTE_COMMAND, event)
    await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent('plain text'))
    expect(screen.getByTestId('value')).not.toHaveTextContent('rich')
    expect(onUploadFiles).not.toHaveBeenCalled()
  })

  it('uploads image-only clipboard data instead of inserting editor content', async () => {
    const editorRef = createRef<LexicalEditor>()
    const onUploadFiles = vi.fn()
    render(<ControlledHost editorRef={editorRef} onUploadFiles={onUploadFiles} />)
    await waitFor(() => expect(editorRef.current).not.toBeNull())
    const image = new File(['px'], 'photo.png', { type: 'image/png' })
    const event = new Event('paste', { cancelable: true }) as ClipboardEvent
    Object.defineProperty(event, 'clipboardData', {
      value: {
        types: ['Files'],
        items: [{ kind: 'file', type: 'image/png', getAsFile: () => image }],
        getData: () => '',
      },
    })
    await dispatchAtEnd(editorRef.current!, PASTE_COMMAND, event)
    expect(onUploadFiles).toHaveBeenCalledWith([image])
    expect(screen.getByTestId('value')).toHaveTextContent('')
  })

  it('normalizes trailing blank lines before creating a paste sidecar', async () => {
    const editorRef = createRef<LexicalEditor>()
    render(<ControlledHost editorRef={editorRef} />)
    await waitFor(() => expect(editorRef.current).not.toBeNull())
    const event = new Event('paste', { cancelable: true }) as ClipboardEvent
    Object.defineProperty(event, 'clipboardData', {
      value: {
        types: ['text/plain'],
        items: [],
        getData: () => 'one\ntwo\nthree\nfour\n\n',
      },
    })
    await dispatchAtEnd(editorRef.current!, PASTE_COMMAND, event)
    await waitFor(() => expect(screen.getByTestId('paste-token-1')).toBeInTheDocument())
    expect(screen.getByTestId('blocks').textContent).toContain('one\\ntwo\\nthree\\nfour')
    expect(screen.getByTestId('blocks').textContent).not.toContain('four\\n\\n')
  })

  it('keeps raw paste inline and preserves trailing blanks', async () => {
    const editorRef = createRef<LexicalEditor>()
    render(<ControlledHost editorRef={editorRef} />)
    await waitFor(() => expect(editorRef.current).not.toBeNull())
    act(() => {
      editorRef.current!.dispatchCommand(
        KEY_MODIFIER_COMMAND,
        new KeyboardEvent('keydown', { key: 'v', ctrlKey: true, shiftKey: true }),
      )
    })
    const payload = 'one\ntwo\nthree\nfour\n\n'
    const event = new Event('paste', { cancelable: true }) as ClipboardEvent
    Object.defineProperty(event, 'clipboardData', {
      value: { types: ['text/plain'], items: [], getData: () => payload },
    })
    await dispatchAtEnd(editorRef.current!, PASTE_COMMAND, event)
    await waitFor(() => expect(screen.getByTestId('value').textContent).toBe(payload))
    expect(screen.getByTestId('blocks')).toHaveTextContent('[]')
    expect(screen.queryByTestId('paste-token-1')).not.toBeInTheDocument()
  })

  it('keeps a large paste inline when showFullPastes is on', async () => {
    const editorRef = createRef<LexicalEditor>()
    render(<ControlledHost editorRef={editorRef} showFullPastes />)
    await waitFor(() => expect(editorRef.current).not.toBeNull())
    const payload = 'one\ntwo\nthree\nfour'
    const event = new Event('paste', { cancelable: true }) as ClipboardEvent
    Object.defineProperty(event, 'clipboardData', {
      value: { types: ['text/plain'], items: [], getData: () => payload },
    })
    await dispatchAtEnd(editorRef.current!, PASTE_COMMAND, event)
    await waitFor(() => expect(screen.getByTestId('value').textContent).toBe(payload))
    expect(screen.getByTestId('blocks')).toHaveTextContent('[]')
    expect(screen.queryByTestId('paste-token-1')).not.toBeInTheDocument()
  })

  it.each(['Enter', ' '])('opens the editable preview from a focused chip on %s and never sends', async key => {
    const editorRef = createRef<LexicalEditor>()
    const onSend = vi.fn()
    render(<ControlledHost initial={formatToken(block)} initialBlocks={[block]} editorRef={editorRef} onSend={onSend} />)
    const chip = screen.getByTestId('paste-token-1')
    chip.focus()
    fireEvent.keyDown(chip, { key })
    const preview = await screen.findByTestId('paste-preview-editor')
    expect(preview).toHaveAttribute('role', 'dialog')
    expect((screen.getByTestId('paste-preview-editor-textarea') as HTMLTextAreaElement).value).toBe(block.content)
    // The accessible name leads with the snippet (what a voice-control user
    // sees), then says what the chip is.
    expect(chip.getAttribute('aria-label')).toBe('alpha · Pasted text · 4 lines')
    // Chip activation must stay the chip's: the same keystroke must never
    // reach the editor's send-on-Enter handler and submit the draft.
    expect(onSend).not.toHaveBeenCalled()
    // Backspace on the focused chip removes it.
    fireEvent.keyDown(chip, { key: 'Backspace' })
    await waitFor(() => expect(screen.queryByTestId('paste-token-1')).not.toBeInTheDocument())
  })

  it('navigates prompt history and restores the draft with canonical caret placement', async () => {
    const editorRef = createRef<LexicalEditor>()
    const controlRef: MutableRefObject<ComposerControl | null> = { current: null }
    render(
      <ControlledHost
        initial="draft"
        editorRef={editorRef}
        controlRef={controlRef}
        sentMessages={['first', 'second']}
      />,
    )
    await waitFor(() => expect(controlRef.current).not.toBeNull())
    act(() => controlRef.current!.setSelection(0))
    act(() => {
      editorRef.current!.dispatchCommand(
        KEY_ARROW_UP_COMMAND,
        new KeyboardEvent('keydown', { key: 'ArrowUp' }),
      )
    })
    await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent('second'))
    await waitFor(() => expect(controlRef.current!.getSelection()).toEqual({ start: 0, end: 0 }))
    act(() => controlRef.current!.setSelection('second'.length))
    act(() => {
      editorRef.current!.dispatchCommand(
        KEY_ARROW_DOWN_COMMAND,
        new KeyboardEvent('keydown', { key: 'ArrowDown' }),
      )
    })
    await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent('draft'))
    await waitFor(() => expect(controlRef.current!.getSelection()).toEqual({ start: 5, end: 5 }))
  })

  it('a history round-trip brings the draft back with its paste block, not a literal marker', async () => {
    // The draft is `intro <pill>`: recalling a sent message shows plain text (no
    // blocks), and ArrowDown past the newest must restore BOTH the marker text and
    // the block record it references — the textarea path keeps `pasteBlocks`
    // untouched across a recall, and the rich composer has to match it. Without
    // the block the marker decodes as literal text and `expandAll` would send
    // `[ Paste #1 · 4 lines ]` instead of the pasted body.
    const editorRef = createRef<LexicalEditor>()
    const controlRef: MutableRefObject<ComposerControl | null> = { current: null }
    const draft = `intro ${formatToken(block)}`
    render(
      <ControlledHost
        initial={draft}
        initialBlocks={[block]}
        editorRef={editorRef}
        controlRef={controlRef}
        sentMessages={['first', 'second']}
      />,
    )
    await waitFor(() => expect(controlRef.current).not.toBeNull())
    expect(screen.getByTestId('paste-token-1')).toBeInTheDocument()

    act(() => controlRef.current!.setSelection(0))
    act(() => {
      editorRef.current!.dispatchCommand(KEY_ARROW_UP_COMMAND, new KeyboardEvent('keydown', { key: 'ArrowUp' }))
    })
    await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent('second'))
    // While a sent message is shown the host holds no blocks: the editor has none.
    await waitFor(() => expect(JSON.parse(screen.getByTestId('blocks').textContent || '[]')).toEqual([]))
    expect(screen.queryByTestId('paste-token-1')).toBeNull()

    act(() => controlRef.current!.setSelection('second'.length))
    act(() => {
      editorRef.current!.dispatchCommand(KEY_ARROW_DOWN_COMMAND, new KeyboardEvent('keydown', { key: 'ArrowDown' }))
    })
    await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent('intro'))
    // The block record rides back with the draft, so the marker is a pill again…
    await waitFor(() => expect(screen.getByTestId('paste-token-1')).toBeInTheDocument())
    const restored = JSON.parse(screen.getByTestId('blocks').textContent || '[]') as PasteBlock[]
    expect(restored).toEqual([block])
    // …and what would be sent is the pasted body, never the marker.
    const value = screen.getByTestId('value').textContent || ''
    expect(value).toBe(draft)
    expect(expandAll(value, restored)).toBe(`intro ${block.content}`)
  })

  it('a slot switch while browsing history drops the parked draft: ↓ in the new slot restores nothing', async () => {
    // Slot A parks `intro <pill>` and shows a sent message; the host then swaps in
    // slot B's draft under a new historyKey. ↓ at the end of B's draft must be an
    // ordinary key, not a restore of A's text and pill into B — the textarea path
    // leaves history mode whenever the value stops being the shown sent message.
    const editorRef = createRef<LexicalEditor>()
    const controlRef: MutableRefObject<ComposerControl | null> = { current: null }
    const draftA = `intro ${formatToken(block)}`
    const props = { editorRef, controlRef, sentMessages: ['first', 'second'] }
    const { rerender } = render(<ControlledHost {...props} initial={draftA} initialBlocks={[block]} historyKey="slot-a" />)
    await waitFor(() => expect(controlRef.current).not.toBeNull())
    act(() => controlRef.current!.setSelection(0))
    act(() => {
      editorRef.current!.dispatchCommand(KEY_ARROW_UP_COMMAND, new KeyboardEvent('keydown', { key: 'ArrowUp' }))
    })
    await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent('second'))

    rerender(<ControlledHost {...props} initial={draftA} initialBlocks={[block]} historyKey="slot-b" restoreValue="slot b draft" />)
    await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent('slot b draft'))
    act(() => controlRef.current!.setSelection('slot b draft'.length))
    act(() => {
      editorRef.current!.dispatchCommand(KEY_ARROW_DOWN_COMMAND, new KeyboardEvent('keydown', { key: 'ArrowDown' }))
    })
    await new Promise<void>(resolve => setTimeout(resolve, 20))
    expect(screen.getByTestId('value')).toHaveTextContent('slot b draft')
    expect(screen.queryByTestId('paste-token-1')).toBeNull()
    expect(screen.getByTestId('value').textContent).not.toContain('intro')
  })

  it('a host value change while browsing history leaves history mode', async () => {
    // Same historyKey, but the host replaces the value (a send-clear, a prefill):
    // the shown text is no longer the recalled message, so ↓ must not bring the
    // parked draft back over the host's new value.
    const editorRef = createRef<LexicalEditor>()
    const controlRef: MutableRefObject<ComposerControl | null> = { current: null }
    const props = { editorRef, controlRef, sentMessages: ['first', 'second'], historyKey: 'slot-a' }
    const { rerender } = render(<ControlledHost {...props} initial="draft" />)
    await waitFor(() => expect(controlRef.current).not.toBeNull())
    act(() => controlRef.current!.setSelection(0))
    act(() => {
      editorRef.current!.dispatchCommand(KEY_ARROW_UP_COMMAND, new KeyboardEvent('keydown', { key: 'ArrowUp' }))
    })
    await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent('second'))

    rerender(<ControlledHost {...props} initial="draft" restoreValue="prefilled by host" />)
    await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent('prefilled by host'))
    act(() => controlRef.current!.setSelection('prefilled by host'.length))
    act(() => {
      editorRef.current!.dispatchCommand(KEY_ARROW_DOWN_COMMAND, new KeyboardEvent('keydown', { key: 'ArrowDown' }))
    })
    await new Promise<void>(resolve => setTimeout(resolve, 20))
    expect(screen.getByTestId('value')).toHaveTextContent('prefilled by host')
  })

  it('undoes the first prompt recall back to a restored draft', async () => {
    const editorRef = createRef<LexicalEditor>()
    const controlRef: MutableRefObject<ComposerControl | null> = { current: null }
    const props = {
      editorRef,
      controlRef,
      sentMessages: ['sent one'],
    }
    const { rerender } = render(<ControlledHost {...props} />)
    await waitFor(() => expect(controlRef.current).not.toBeNull())
    let canUndo = false
    const unregisterUndo = editorRef.current!.registerCommand(
      CAN_UNDO_COMMAND,
      value => { canUndo = value; return false },
      COMMAND_PRIORITY_LOW,
    )

    act(() => controlRef.current!.setSelection(0))
    rerender(<ControlledHost {...props} restoreValue="draft" />)
    await waitFor(() => expect(screen.getByTestId('value').textContent).toBe('draft'))
    act(() => {
      editorRef.current!.dispatchCommand(
        KEY_ARROW_UP_COMMAND,
        new KeyboardEvent('keydown', { key: 'ArrowUp' }),
      )
    })
    await waitFor(() => expect(screen.getByTestId('value').textContent).toBe('sent one'))
    await waitFor(() => expect(canUndo).toBe(true))

    act(() => { editorRef.current!.dispatchCommand(UNDO_COMMAND, undefined) })
    await waitFor(() => expect(screen.getByTestId('value').textContent).toBe('draft'))
    unregisterUndo()
  })

  it('undoes the first prompt recall after moving an equal-valued draft to another slot', async () => {
    const editorRef = createRef<LexicalEditor>()
    const controlRef: MutableRefObject<ComposerControl | null> = { current: null }
    const props = {
      initial: 'draft',
      editorRef,
      controlRef,
      sentMessages: ['sent one'],
    }
    const { rerender } = render(<ControlledHost {...props} historyKey="slot-a" />)
    await waitFor(() => expect(controlRef.current).not.toBeNull())
    let canUndo = false
    const unregisterUndo = editorRef.current!.registerCommand(
      CAN_UNDO_COMMAND,
      value => { canUndo = value; return false },
      COMMAND_PRIORITY_LOW,
    )

    act(() => controlRef.current!.setSelection(0))
    rerender(<ControlledHost {...props} historyKey="slot-b" />)
    act(() => {
      editorRef.current!.dispatchCommand(
        KEY_ARROW_UP_COMMAND,
        new KeyboardEvent('keydown', { key: 'ArrowUp' }),
      )
    })
    await waitFor(() => expect(screen.getByTestId('value').textContent).toBe('sent one'))
    await waitFor(() => expect(canUndo).toBe(true))

    act(() => { editorRef.current!.dispatchCommand(UNDO_COMMAND, undefined) })
    await waitFor(() => expect(screen.getByTestId('value').textContent).toBe('draft'))
    unregisterUndo()
  })

  it('undoes a prompt recall in one step back to the typed draft', async () => {
    const editorRef = createRef<LexicalEditor>()
    const controlRef: MutableRefObject<ComposerControl | null> = { current: null }
    render(
      <ControlledHost
        initial="draft"
        editorRef={editorRef}
        controlRef={controlRef}
        sentMessages={['sent one']}
      />,
    )
    await waitFor(() => expect(controlRef.current).not.toBeNull())
    await dispatchAtEnd(editorRef.current!, CONTROLLED_TEXT_INSERTION_COMMAND, ' typed')
    await waitFor(() => expect(screen.getByTestId('value').textContent).toBe('draft typed'))

    act(() => controlRef.current!.setSelection(0))
    act(() => {
      editorRef.current!.dispatchCommand(
        KEY_ARROW_UP_COMMAND,
        new KeyboardEvent('keydown', { key: 'ArrowUp' }),
      )
    })
    await waitFor(() => expect(screen.getByTestId('value').textContent).toBe('sent one'))

    act(() => { editorRef.current!.dispatchCommand(UNDO_COMMAND, undefined) })
    await waitFor(() => expect(screen.getByTestId('value').textContent).toBe('draft typed'))
  })

  it('closes an open preview when the host swaps in another session whose draft has the same paste seq', async () => {
    // Session A and session B both hold a `Paste #1`; the block ids differ.
    // Switching sessions while A's preview is open replaces the controlled
    // value + blocks underneath the popover. It must close rather than rebind
    // to B's block — otherwise a Save would write A's edit into B's paste.
    const other: PasteBlock = { id: 'paste-B', seq: 1, lines: 2, content: 'from B\nnot A' }
    const onBlocksChange = vi.fn()
    const props = {
      onChange: vi.fn(),
      onBlocksChange,
      onSend: vi.fn(),
      ariaLabel: 'Message input',
      placeholder: 'Write a message',
    }
    const { rerender } = render(<LexicalComposerInput value={formatToken(block)} blocks={[block]} {...props} />)
    fireEvent.click(screen.getByTestId('paste-token-1'))
    const textarea = (await screen.findByTestId('paste-preview-editor-textarea')) as HTMLTextAreaElement
    expect(textarea.value).toBe(block.content)

    rerender(<LexicalComposerInput value={formatToken(other)} blocks={[other]} {...props} />)

    await waitFor(() => expect(screen.queryByTestId('paste-preview-editor')).toBeNull())
    // B's pill is on screen with B's content untouched — nothing from A landed on it.
    expect(screen.getByTestId('paste-token-1')).toBeInTheDocument()
    expect(onBlocksChange).not.toHaveBeenCalledWith(expect.arrayContaining([expect.objectContaining({ id: 'paste-B', content: block.content })]))
  })

  describe('a value that holds the same marker twice', () => {
    // A value can hold `[ Paste #1 · … ]` twice when the user copies or types
    // marker text. Only the first occurrence is backed by the one available
    // record; later occurrences stay ordinary text and are sent literally.
    const twice = `x${formatToken(block)}y${formatToken(block)}z`

    it('renders one re-sequenced pill and leaves the later occurrence as literal text', async () => {
      render(<ControlledHost initial={twice} initialBlocks={[block]} />)
      const canonicalValue = `x[ Paste #2 · ${block.lines} lines ]y${formatToken(block)}z`
      await waitFor(() => expect(screen.getByTestId('value').textContent).toBe(canonicalValue))
      const blocks = JSON.parse(screen.getByTestId('blocks').textContent!) as PasteBlock[]
      expect(blocks).toEqual([
        expect.objectContaining({ seq: 2, lines: block.lines, content: block.content }),
      ])
      expect(blocks[0].id).not.toBe(block.id)
      expect(screen.getByTestId('paste-token-2')).toBeInTheDocument()
      expect(screen.queryByTestId('paste-token-1')).toBeNull()
      expect(expandAll(canonicalValue, blocks))
        .toBe(`x${block.content}y${formatToken(block)}z`)
    })

    it('the ✕ removes the backed pill but preserves the literal occurrence', async () => {
      render(<ControlledHost initial={twice} initialBlocks={[block]} />)
      const pill = await screen.findByTestId('paste-token-2')
      fireEvent.click(within(pill).getByRole('button', { name: 'Remove pasted text' }))
      await waitFor(() => expect(screen.getByTestId('value').textContent).toBe(`xy${formatToken(block)}z`))
      expect(JSON.parse(screen.getByTestId('blocks').textContent!)).toEqual([])
      expect(screen.queryByTestId('paste-token-2')).toBeNull()
    })

    it('saving the preview edits only the re-sequenced pill and leaves the later marker literal', async () => {
      render(<ControlledHost initial={twice} initialBlocks={[block]} />)
      fireEvent.click(await screen.findByTestId('paste-token-2'))
      const textarea = (await screen.findByTestId('paste-preview-editor-textarea')) as HTMLTextAreaElement
      expect(textarea.value).toBe(block.content)
      fireEvent.change(textarea, { target: { value: 'one\ntwo' } })
      fireEvent.click(screen.getByRole('button', { name: 'Save' }))
      await waitFor(() => expect(screen.getByTestId('value').textContent).toBe(`x[ Paste #2 · 2 lines ]y${formatToken(block)}z`))
      const value = screen.getByTestId('value').textContent!
      const blocks = JSON.parse(screen.getByTestId('blocks').textContent!) as PasteBlock[]
      expect(blocks).toEqual([
        expect.objectContaining({ seq: 2, lines: 2, content: 'one\ntwo' }),
      ])
      expect(expandAll(value, blocks)).toBe(`xone\ntwoy${formatToken(block)}z`)
      expect(screen.queryByTestId('paste-preview-editor')).toBeNull()
    })

    it('a restored draft whose block list already holds two divergent same-seq records keeps both pastes', async () => {
      // The state a draft persisted before the seq invariant can carry: two pills
      // that shared seq 1 and were edited apart. Records pair with occurrences in
      // order, so the first paste keeps its content instead of being resolved
      // through the last record.
      const edited: PasteBlock = { id: 'paste-1-edited', seq: 1, lines: 1, content: 'edited' }
      const divergent = `x${formatToken(block)}y${formatToken(edited)}z`
      render(<ControlledHost initial={divergent} initialBlocks={[block, edited]} />)
      const canonical = { ...edited, seq: 2 }
      await waitFor(() => expect(screen.getByTestId('value').textContent).toBe(`x${formatToken(block)}y${formatToken(canonical)}z`))
      const blocks = JSON.parse(screen.getByTestId('blocks').textContent!) as PasteBlock[]
      expect(blocks).toEqual([block, canonical])
      expect(within(screen.getByTestId('paste-token-1')).getByTestId('paste-chip-snippet')).toHaveTextContent('alpha')
      expect(within(screen.getByTestId('paste-token-2')).getByTestId('paste-chip-snippet')).toHaveTextContent('edited')
      expect(expandAll(screen.getByTestId('value').textContent!, blocks)).toBe(`x${block.content}yeditedz`)
    })

    it('typing a literal marker next to an existing pill re-sequences the pill (TextNode trigger)', async () => {
      // Steady state, through the real deferred transform registration: a pill is
      // already mounted; the user types its exact marker text after it. The
      // TextNode transform must catch the collision and give the PILL a fresh seq
      // so it keeps its block, while the typed literal stays `#1` and inert.
      const editorRef = createRef<LexicalEditor>()
      render(<ControlledHost initial={formatToken(block)} initialBlocks={[block]} editorRef={editorRef} />)
      await waitFor(() => expect(editorRef.current).not.toBeNull())
      await waitFor(() => expect(screen.getByTestId('value').textContent).toBe(formatToken(block)))

      const literal = ` ${formatToken(block)}`
      await dispatchAtEnd(editorRef.current!, CONTROLLED_TEXT_INSERTION_COMMAND, literal)

      await waitFor(() =>
        expect(screen.getByTestId('value').textContent).toBe(`[ Paste #2 · ${block.lines} lines ] ${formatToken(block)}`),
      )
      const value = screen.getByTestId('value').textContent!
      const blocks = JSON.parse(screen.getByTestId('blocks').textContent!) as PasteBlock[]
      expect(blocks).toEqual([
        expect.objectContaining({ seq: 2, lines: block.lines, content: block.content }),
      ])
      expect(blocks[0].id).not.toBe(block.id)
      // The pill moved to seq 2; the typed literal stays #1 and unbacked, so
      // expansion puts the content at the pill's position and leaves the literal.
      expect(expandAll(value, blocks)).toBe(`${block.content} ${formatToken(block)}`)
    })
  })

  describe('the preview writes through to the pill', () => {
    /** Listen to CAN_UNDO / CAN_REDO on the editor and expose the latest values. */
    function trackHistory(editor: LexicalEditor) {
      const state = { canUndo: false, canRedo: false }
      const off = [
        editor.registerCommand(CAN_UNDO_COMMAND, value => { state.canUndo = value; return false }, COMMAND_PRIORITY_LOW),
        editor.registerCommand(CAN_REDO_COMMAND, value => { state.canRedo = value; return false }, COMMAND_PRIORITY_LOW),
      ]
      return { state, unregister: () => off.forEach(fn => fn()) }
    }
    const hostBlocks = () => JSON.parse(screen.getByTestId('blocks').textContent!) as PasteBlock[]

    it('every keystroke in the open preview reaches the host block list before any Save', async () => {
      // The finding this closes: the panel used to hold the edit alone until
      // Save, so a reload / Back / session switch that unmounted it mid-edit
      // left the draft with the pre-edit paste. Now the draft is current at
      // every keystroke — and the chip re-labels itself as the user types.
      render(<ControlledHost initial={`x${formatToken(block)}y`} initialBlocks={[block]} />)
      fireEvent.click(screen.getByTestId('paste-token-1'))
      const textarea = (await screen.findByTestId('paste-preview-editor-textarea')) as HTMLTextAreaElement
      fireEvent.change(textarea, { target: { value: 'one' } })
      fireEvent.change(textarea, { target: { value: 'one\ntwo' } })
      await waitFor(() => expect(hostBlocks()[0]).toMatchObject({ id: 'paste-1', seq: 1, lines: 2, content: 'one\ntwo' }))
      // Still open, nothing committed, and the value text (the marker) is unchanged.
      expect(screen.getByTestId('paste-preview-editor')).toBeInTheDocument()
      expect(textarea.value).toBe('one\ntwo')
      expect(screen.getByTestId('value').textContent).toBe(`x${formatToken({ ...block, lines: 2 })}y`)
      expect(within(screen.getByTestId('paste-token-1')).getByTestId('paste-chip-snippet')).toHaveTextContent('one')
      // What the host would persist right now already carries the edit.
      expect(expandAll(screen.getByTestId('value').textContent!, hostBlocks())).toBe('xone\ntwoy')
    })

    it('Cancel puts the content the panel opened on back into the pill', async () => {
      render(<ControlledHost initial={formatToken(block)} initialBlocks={[block]} />)
      fireEvent.click(screen.getByTestId('paste-token-1'))
      const textarea = (await screen.findByTestId('paste-preview-editor-textarea')) as HTMLTextAreaElement
      fireEvent.change(textarea, { target: { value: 'scratch' } })
      await waitFor(() => expect(hostBlocks()[0].content).toBe('scratch'))
      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
      await waitFor(() => expect(hostBlocks()).toEqual([block]))
      expect(screen.queryByTestId('paste-preview-editor')).toBeNull()
      expect(within(screen.getByTestId('paste-token-1')).getByTestId('paste-chip-snippet')).toHaveTextContent('alpha')
    })

    it('a second Escape on a dirty panel also restores the original', async () => {
      render(<ControlledHost initial={formatToken(block)} initialBlocks={[block]} />)
      fireEvent.click(screen.getByTestId('paste-token-1'))
      const textarea = (await screen.findByTestId('paste-preview-editor-textarea')) as HTMLTextAreaElement
      fireEvent.change(textarea, { target: { value: 'scratch' } })
      await waitFor(() => expect(hostBlocks()[0].content).toBe('scratch'))
      fireEvent.keyDown(textarea, { key: 'Escape' })
      // First Escape: still open, hint showing, pill still holds the live edit.
      expect(screen.getByTestId('paste-preview-editor-unsaved')).toBeInTheDocument()
      expect(hostBlocks()[0].content).toBe('scratch')
      fireEvent.keyDown(textarea, { key: 'Escape' })
      await waitFor(() => expect(hostBlocks()).toEqual([block]))
      expect(screen.queryByTestId('paste-preview-editor')).toBeNull()
    })

    it('a saved edit is ONE undo step — the interim keystrokes are not recorded — and a cancelled edit records nothing', async () => {
      const editorRef = createRef<LexicalEditor>()
      render(<ControlledHost initial={formatToken(block)} initialBlocks={[block]} editorRef={editorRef} />)
      await waitFor(() => expect(editorRef.current).not.toBeNull())
      const { state, unregister } = trackHistory(editorRef.current!)
      const canUndoBefore = state.canUndo

      // Cancelled edit: three keystrokes, then Cancel → no history entry.
      fireEvent.click(screen.getByTestId('paste-token-1'))
      let textarea = (await screen.findByTestId('paste-preview-editor-textarea')) as HTMLTextAreaElement
      for (const v of ['a', 'ab', 'abc']) fireEvent.change(textarea, { target: { value: v } })
      await waitFor(() => expect(hostBlocks()[0].content).toBe('abc'))
      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
      await waitFor(() => expect(hostBlocks()).toEqual([block]))
      expect(state.canUndo).toBe(canUndoBefore)

      // Saved edit: three keystrokes, then Save → exactly one entry.
      fireEvent.click(screen.getByTestId('paste-token-1'))
      textarea = (await screen.findByTestId('paste-preview-editor-textarea')) as HTMLTextAreaElement
      for (const v of ['o', 'on', 'one']) fireEvent.change(textarea, { target: { value: v } })
      await waitFor(() => expect(hostBlocks()[0].content).toBe('one'))
      fireEvent.click(screen.getByRole('button', { name: 'Save' }))
      await waitFor(() => expect(screen.queryByTestId('paste-preview-editor')).toBeNull())
      await waitFor(() => expect(state.canUndo).toBe(true))
      act(() => { editorRef.current!.dispatchCommand(UNDO_COMMAND, undefined) })
      // One undo lands on the ORIGINAL, not on 'on' or 'o'.
      await waitFor(() => expect(hostBlocks()[0].content).toBe(block.content))
      await waitFor(() => expect(state.canRedo).toBe(true))
      act(() => { editorRef.current!.dispatchCommand(REDO_COMMAND, undefined) })
      await waitFor(() => expect(hostBlocks()[0].content).toBe('one'))
      unregister()
    })

    it('the open preview follows its pill when the window resizes', async () => {
      render(<ControlledHost initial={formatToken(block)} initialBlocks={[block]} />)
      const chip = screen.getByTestId('paste-token-1')
      const at = (left: number, top: number) => ({ left, top, right: left + 100, bottom: top + 20, width: 100, height: 20, x: left, y: top, toJSON: () => ({}) }) as DOMRect
      const spy = vi.spyOn(chip, 'getBoundingClientRect').mockReturnValue(at(42, 18))
      fireEvent.click(chip)
      const preview = await screen.findByTestId('paste-preview-editor')
      expect(preview).toHaveStyle({ left: '42px' })
      // The composer re-wraps and the pill lands elsewhere; the panel re-anchors.
      spy.mockReturnValue(at(120, 18))
      fireEvent(window, new Event('resize'))
      await waitFor(() => expect(screen.getByTestId('paste-preview-editor')).toHaveStyle({ left: '120px' }))
    })
  })
})
