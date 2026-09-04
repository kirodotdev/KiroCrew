import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createRef, useState } from 'react'
import type { MutableRefObject, RefObject } from 'react'
import { $getRoot, $getSelection, $isRangeSelection, CAN_REDO_COMMAND, CAN_UNDO_COMMAND, COMMAND_PRIORITY_LOW, CONTROLLED_TEXT_INSERTION_COMMAND, COPY_COMMAND, KEY_ARROW_DOWN_COMMAND, KEY_ARROW_UP_COMMAND, KEY_ENTER_COMMAND, KEY_MODIFIER_COMMAND, PASTE_COMMAND, REDO_COMMAND, UNDO_COMMAND, type LexicalCommand, type LexicalEditor } from 'lexical'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import LexicalComposerInput from '../components/LexicalComposerInput'
import type { ComposerControl } from '../components/composerControl'
import { formatToken, type PasteBlock } from '../utils/pasteTokens'

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
    expect(chip).toHaveTextContent('Paste #1 · 4 lines')
    expect(chip).not.toHaveTextContent('[ Paste')
    expect(chip).toHaveAttribute('contenteditable', 'false')
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
    vi.useFakeTimers()
    render(<ControlledHost initial={formatToken(block)} initialBlocks={[block]} />)
    const chip = screen.getByTestId('paste-token-1')
    const host = chip.parentElement as HTMLElement
    const rect = { left: 42, top: 18, right: 142, bottom: 38, width: 100, height: 20, x: 42, y: 18, toJSON: () => ({}) }
    vi.spyOn(host, 'getBoundingClientRect').mockReturnValue(rect as DOMRect)
    fireEvent.mouseEnter(chip)
    await vi.advanceTimersByTimeAsync(300)
    const preview = screen.getByTestId('lexical-paste-preview-1')
    expect(preview).toHaveStyle({ left: '42px', top: '42px' })
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

  it('copies and cuts selected paste chips as expanded text', async () => {
    const user = userEvent.setup()
    render(<ControlledHost initial={formatToken(block)} initialBlocks={[block]} />)
    const chip = screen.getByTestId('paste-token-1')
    await user.click(chip)
    const clipboard = { setData: vi.fn() }
    fireEvent.copy(screen.getByRole('textbox'), { clipboardData: clipboard })
    expect(clipboard.setData).toHaveBeenCalledWith('text/plain', block.content)
    fireEvent.cut(screen.getByRole('textbox'), { clipboardData: clipboard })
    await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent(''))
    expect(screen.getByTestId('blocks')).toHaveTextContent('[]')
  })

  it('deletes a selected paste chip atomically', async () => {
    const user = userEvent.setup()
    render(<ControlledHost initial={`x${formatToken(block)}y`} initialBlocks={[block]} />)
    await user.click(screen.getByTestId('paste-token-1'))
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

  it.each(['Enter', ' '])('opens a selected token preview on %s activation', async key => {
    const editorRef = createRef<LexicalEditor>()
    render(<ControlledHost initial={formatToken(block)} initialBlocks={[block]} editorRef={editorRef} />)
    const chip = screen.getByTestId('paste-token-1')
    chip.focus()
    fireEvent.keyDown(chip, { key })
    expect(await screen.findByTestId('lexical-paste-preview-1')).toBeInTheDocument()
    expect(chip).toHaveAttribute('aria-expanded', 'true')
    expect(chip.getAttribute('aria-label')).toContain(chip.getAttribute('title') || '')
    expect(chip.getAttribute('aria-label')).toContain('Paste #1 · 4 lines')
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

})
