import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createRef, useState } from 'react'
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
}: {
  initial?: string
  initialBlocks?: PasteBlock[]
  onSend?: () => void
  editorRef?: RefObject<LexicalEditor | null>
  controlRef?: MutableRefObject<ComposerControl | null>
  onUploadFiles?: (files: File[]) => void
  onSelectionChange?: (selection: { start: number; end: number }) => void
  sentMessages?: string[]
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
        editorRef={editorRef}
        controlRef={controlRef}
        onUploadFiles={onUploadFiles}
        onSelectionChange={onSelectionChange}
        sentMessages={sentMessages}
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
    // `$replaceComposerValue` appends one node per marker occurrence, so a value
    // holding `[ Paste #1 · … ]` twice (a restored draft, a small paste that
    // contains the marker text) rehydrates as two nodes sharing seq AND id. A
    // marker resolves by seq alone, so the pair must be split the moment it
    // appears (`PasteSeqInvariantPlugin`) — otherwise `expandAll` writes one
    // block into both markers — and every pill action must act on the clicked
    // NODE, never on "the pill with this seq" (GPT reviews on #11100).
    const twice = `x${formatToken(block)}y${formatToken(block)}z`
    const second = { ...block, seq: 2 }

    it('re-sequences the later twin and hands the host the rewritten value + blocks', async () => {
      render(<ControlledHost initial={twice} initialBlocks={[block]} />)
      await waitFor(() => expect(screen.getByTestId('value').textContent).toBe(`x${formatToken(block)}y${formatToken(second)}z`))
      const blocks = JSON.parse(screen.getByTestId('blocks').textContent!) as PasteBlock[]
      expect(blocks).toHaveLength(2)
      expect(blocks[0]).toEqual(block)
      expect(blocks[1]).toEqual(expect.objectContaining({ seq: 2, lines: block.lines, content: block.content }))
      expect(blocks[1].id).not.toBe(block.id)
      expect(screen.getByTestId('paste-token-1')).toBeInTheDocument()
      expect(screen.getByTestId('paste-token-2')).toBeInTheDocument()
    })

    it('the ✕ removes only the clicked pill; its twin survives', async () => {
      render(<ControlledHost initial={twice} initialBlocks={[block]} />)
      const later = await screen.findByTestId('paste-token-2')
      fireEvent.click(within(later).getByRole('button', { name: 'Remove pasted text' }))
      await waitFor(() => expect(screen.getByTestId('value').textContent).toBe(`x${formatToken(block)}yz`))
      expect(JSON.parse(screen.getByTestId('blocks').textContent!)).toEqual([block])
      expect(screen.getByTestId('paste-token-1')).toBeInTheDocument()
    })

    it('saving the preview edits only the clicked pill, and the send expansion carries both contents', async () => {
      render(<ControlledHost initial={twice} initialBlocks={[block]} />)
      fireEvent.click(await screen.findByTestId('paste-token-2'))
      const textarea = (await screen.findByTestId('paste-preview-editor-textarea')) as HTMLTextAreaElement
      expect(textarea.value).toBe(block.content)
      fireEvent.change(textarea, { target: { value: 'one\ntwo' } })
      fireEvent.click(screen.getByRole('button', { name: 'Save' }))
      const edited = { ...second, lines: 2, content: 'one\ntwo' }
      await waitFor(() => expect(screen.getByTestId('value').textContent).toBe(`x${formatToken(block)}y${formatToken(edited)}z`))
      const value = screen.getByTestId('value').textContent!
      const blocks = JSON.parse(screen.getByTestId('blocks').textContent!) as PasteBlock[]
      expect(blocks.map(b => b.content)).toEqual([block.content, 'one\ntwo'])
      // What the host submits: each marker expands to ITS block — the original
      // paste is not replaced by the edited twin.
      expect(expandAll(value, blocks)).toBe(`x${block.content}yone\ntwoz`)
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
