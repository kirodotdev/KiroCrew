import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createRef, useState } from 'react'
import type { MutableRefObject, RefObject } from 'react'
import { $getRoot, $getSelection, $isRangeSelection, CAN_REDO_COMMAND, CAN_UNDO_COMMAND, COMMAND_PRIORITY_LOW, CONTROLLED_TEXT_INSERTION_COMMAND, COPY_COMMAND, KEY_ARROW_DOWN_COMMAND, KEY_ARROW_UP_COMMAND, KEY_BACKSPACE_COMMAND, KEY_DELETE_COMMAND, KEY_ENTER_COMMAND, KEY_MODIFIER_COMMAND, PASTE_COMMAND, REDO_COMMAND, UNDO_COMMAND, type LexicalCommand, type LexicalEditor } from 'lexical'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import LexicalComposerInput from '../components/LexicalComposerInput'
import { IS_MAC } from '../hooks/useKeyboardShortcuts'
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
  historyScope,
  showFullPastes,
  readOnly,
  onEditLastRequest,
  onMentionKey,
  onEndUndoBurst,
}: {
  initial?: string
  initialBlocks?: PasteBlock[]
  onSend?: () => void
  editorRef?: RefObject<LexicalEditor | null>
  controlRef?: MutableRefObject<ComposerControl | null>
  onUploadFiles?: (files: File[]) => void
  onSelectionChange?: (selection: { start: number; end: number }) => void
  /** Prompt texts; the host wraps each one as an id-less history entry. */
  sentMessages?: string[]
  historyScope?: string
  showFullPastes?: boolean
  readOnly?: boolean
  onEditLastRequest?: () => void
  onMentionKey?: (text: string, selStart: number, selEnd: number, key: string, mods: { meta: boolean; ctrl: boolean; alt: boolean; shift: boolean }) => { value: string; caret: number } | null
  onEndUndoBurst?: () => void
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
        showFullPastes={showFullPastes}
        readOnly={readOnly}
        onSend={onSend}
        ariaLabel="Message input"
        placeholder="Write a message"
        editorRef={editorRef}
        controlRef={controlRef}
        onUploadFiles={onUploadFiles}
        onSelectionChange={onSelectionChange}
        sentMessages={sentMessages?.map(text => ({ text }))}
        historyScope={historyScope}
        onEditLastRequest={onEditLastRequest}
        onMentionKey={onMentionKey}
        onEndUndoBurst={onEndUndoBurst}
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
    await waitFor(() => expect(screen.getByTestId('value').textContent).toMatch(/\[ Paste #1(?:\u2063[\u200b\u200c]+\u2063)? · 4 lines \]/))
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
    fireEvent.pointerEnter(chip, { pointerType: 'mouse' })
    await vi.advanceTimersByTimeAsync(300)
    const preview = screen.getByTestId('lexical-paste-preview-1')
    expect(preview).toHaveStyle({ left: '42px', top: '42px' })
  })

  it('a touch tap does not open the preview on hover; the tap itself toggles it', async () => {
    vi.useFakeTimers()
    render(<ControlledHost initial={formatToken(block)} initialBlocks={[block]} />)
    const chip = screen.getByTestId('paste-token-1')
    fireEvent.pointerEnter(chip, { pointerType: 'touch' })
    fireEvent.mouseEnter(chip)
    await vi.advanceTimersByTimeAsync(300)
    expect(screen.queryByTestId('lexical-paste-preview-1')).toBeNull()
    fireEvent.click(chip)
    await vi.advanceTimersByTimeAsync(0)
    expect(screen.getByTestId('lexical-paste-preview-1')).toBeInTheDocument()
  })

  it('keyboard focus still previews', async () => {
    vi.useFakeTimers()
    render(<ControlledHost initial={formatToken(block)} initialBlocks={[block]} />)
    fireEvent.focus(screen.getByTestId('paste-token-1'))
    await vi.advanceTimersByTimeAsync(300)
    expect(screen.getByTestId('lexical-paste-preview-1')).toBeInTheDocument()
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

  // #23148: the Lexical composer (reached via Style Markdown While Typing) must
  // expand a collapsed paste on double-click, matching the textarea composer.
  it('expands a paste chip to its full text on double-click and drops the block', async () => {
    render(<ControlledHost initial={`x${formatToken(block)}y`} initialBlocks={[block]} />)
    fireEvent.doubleClick(screen.getByTestId('paste-token-1'))
    await waitFor(() => expect(screen.getByTestId('value').textContent).toBe(`x${block.content}y`))
    expect(screen.queryByTestId('paste-token-1')).not.toBeInTheDocument()
    expect(screen.getByTestId('blocks')).toHaveTextContent('[]')
  })

  // A real double-click is click, click, dblclick: userEvent.dblClick sends the
  // whole sequence, so each click handler runs before the dblclick one does.
  // While read-only, none of them may touch the editor state.
  it('does not change the editor on a real double-click while read-only', async () => {
    const user = userEvent.setup()
    const editorRef = createRef<LexicalEditor>()
    const { rerender } = render(
      <ControlledHost initial={`x${formatToken(block)}y`} initialBlocks={[block]} editorRef={editorRef} />,
    )
    rerender(
      <ControlledHost initial={`x${formatToken(block)}y`} initialBlocks={[block]} editorRef={editorRef} readOnly />,
    )
    await waitFor(() => expect(editorRef.current?.isEditable()).toBe(false))
    const chip = screen.getByTestId('paste-token-1')
    const blocksBefore = screen.getByTestId('blocks').textContent
    const updateSpy = vi.spyOn(editorRef.current!, 'update')
    await user.dblClick(chip)
    expect(updateSpy).not.toHaveBeenCalled()
    expect(screen.getByTestId('value').textContent).toBe(`x${formatToken(block)}y`)
    expect(screen.getByTestId('blocks').textContent).toBe(blocksBefore)
    expect(blocksBefore).toContain(block.id)
    expect(screen.getByTestId('paste-token-1')).toBeInTheDocument()
  })

  it('a single click on a paste chip still only selects it, never expands', async () => {
    const user = userEvent.setup()
    render(<ControlledHost initial={`x${formatToken(block)}y`} initialBlocks={[block]} />)
    await user.click(screen.getByTestId('paste-token-1'))
    expect(screen.getByTestId('paste-token-1')).toBeInTheDocument()
    expect(screen.getByTestId('value').textContent).toBe(`x${formatToken(block)}y`)
  })

  // #18186: the Lexical composer must delete a staged @mention as one atomic
  // unit, matching the textarea composer (#17929). The editor hands the flat
  // text + flat selection to the shared `onMentionKey` handler (mentionKeyEdit)
  // and applies the whole-value result. These exercise the wiring + the
  // range-removal/caret application; mentionKeyEdit's own span logic is covered
  // by the textarea/fileTokens suites. The stub below reproduces exactly what
  // mentionKeyEdit returns for the single staged mention `@src/main.ts`.
  describe('atomic staged-mention delete (#18186)', () => {
    const MENTION = '@src/main.ts'
    // A faithful one-mention stand-in for mentionKeyEdit: find the mention span,
    // and for a Backspace/Delete on/next to it (or a selection overlapping it)
    // return the text with the whole mention (and one adjacent space) removed.
    // The word-delete chord (Ctrl/Alt, not Cmd) deletes the adjacent mention
    // whole and also eats whitespace between the caret and the token, mirroring
    // the real mentionKeyEdit's `wordDelete` branch.
    const mentionKeyEditForSrcMain = (
      text: string, selStart: number, selEnd: number, key: string,
      mods: { meta: boolean; ctrl: boolean; alt: boolean; shift: boolean },
    ): { value: string; caret: number } | null => {
      if (key !== 'Backspace' && key !== 'Delete') return null
      if (key === 'Delete' && mods.shift && selStart !== selEnd) return null
      const wordDelete = (mods.alt || mods.ctrl) && !mods.meta
      if (mods.meta) return null
      if (!wordDelete && (mods.alt || mods.ctrl)) return null
      const at = text.indexOf(MENTION)
      if (at < 0) return null
      const end = at + MENTION.length
      const hasSelection = selStart !== selEnd
      const onlySpaceBetween = (a: number, b: number) => {
        for (let i = a; i < b; i++) if (text[i] !== ' ' && text[i] !== '\t') return false
        return true
      }
      const take = hasSelection
        ? selStart < end && selEnd > at
        : wordDelete
          ? key === 'Backspace'
            ? (selStart > at && selStart <= end) || (selStart > end && onlySpaceBetween(end, selStart))
            : (selStart >= at && selStart < end) || (selStart < at && onlySpaceBetween(selStart, at))
          : key === 'Backspace'
            ? selStart > at && selStart <= end
            : selStart >= at && selStart < end
      if (!take) return null
      if (!hasSelection && wordDelete) {
        if (key === 'Backspace' && selStart > end) {
          return { value: text.slice(0, at) + text.slice(selStart), caret: at }
        }
        if (key === 'Delete' && selStart < at) {
          return { value: text.slice(0, selStart) + text.slice(end), caret: selStart }
        }
      }
      let s = hasSelection ? Math.min(selStart, at) : at
      let e = hasSelection ? Math.max(selEnd, end) : end
      if (e < text.length && text[e] === ' ') e++
      else if (s > 0 && text[s - 1] === ' ') s--
      return { value: text.slice(0, s) + text.slice(e), caret: s }
    }

    const setCaret = async (controlRef: MutableRefObject<ComposerControl | null>, pos: number) => {
      await waitFor(() => expect(controlRef.current).not.toBeNull())
      act(() => controlRef.current!.setSelection(pos, pos, { focus: true }))
    }

    it('Backspace with the caret just past a mention removes the whole mention and its chip would unstage', async () => {
      const editorRef = createRef<LexicalEditor>()
      const controlRef: MutableRefObject<ComposerControl | null> = { current: null }
      const initial = `hi ${MENTION} there`
      render(<ControlledHost initial={initial} editorRef={editorRef} controlRef={controlRef} onMentionKey={mentionKeyEditForSrcMain} />)
      await waitFor(() => expect(editorRef.current).not.toBeNull())
      // Caret immediately after `@src/main.ts` (index 3 + 12 == 15).
      await setCaret(controlRef, 3 + MENTION.length)
      const event = new KeyboardEvent('keydown', { key: 'Backspace', cancelable: true })
      act(() => { editorRef.current!.dispatchCommand(KEY_BACKSPACE_COMMAND, event) })
      // The whole mention (and one adjacent space) is gone — never the
      // half-reference `@src/main.t` a one-character delete would leave.
      await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent('hi there'))
    })

    it('Backspace with the caret inside a mention removes it whole', async () => {
      const editorRef = createRef<LexicalEditor>()
      const controlRef: MutableRefObject<ComposerControl | null> = { current: null }
      const initial = `hi ${MENTION} there`
      render(<ControlledHost initial={initial} editorRef={editorRef} controlRef={controlRef} onMentionKey={mentionKeyEditForSrcMain} />)
      await waitFor(() => expect(editorRef.current).not.toBeNull())
      await setCaret(controlRef, 3 + 6) // mid-mention
      const event = new KeyboardEvent('keydown', { key: 'Backspace', cancelable: true })
      act(() => { editorRef.current!.dispatchCommand(KEY_BACKSPACE_COMMAND, event) })
      await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent('hi there'))
    })

    it('Delete with the caret just before a mention removes it whole', async () => {
      const editorRef = createRef<LexicalEditor>()
      const controlRef: MutableRefObject<ComposerControl | null> = { current: null }
      const initial = `hi ${MENTION} there`
      render(<ControlledHost initial={initial} editorRef={editorRef} controlRef={controlRef} onMentionKey={mentionKeyEditForSrcMain} />)
      await waitFor(() => expect(editorRef.current).not.toBeNull())
      await setCaret(controlRef, 3) // at the `@`
      const event = new KeyboardEvent('keydown', { key: 'Delete', cancelable: true })
      act(() => { editorRef.current!.dispatchCommand(KEY_DELETE_COMMAND, event) })
      await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent('hi there'))
    })

    it('a selection overlapping a mention removes the whole mention, not a fragment', async () => {
      const editorRef = createRef<LexicalEditor>()
      const controlRef: MutableRefObject<ComposerControl | null> = { current: null }
      const initial = `hi ${MENTION} there`
      render(<ControlledHost initial={initial} editorRef={editorRef} controlRef={controlRef} onMentionKey={mentionKeyEditForSrcMain} />)
      await waitFor(() => expect(editorRef.current).not.toBeNull())
      // Select from inside "hi " across into the middle of the mention.
      await waitFor(() => expect(controlRef.current).not.toBeNull())
      act(() => controlRef.current!.setSelection(1, 3 + 6, { focus: true }))
      const event = new KeyboardEvent('keydown', { key: 'Backspace', cancelable: true })
      act(() => { editorRef.current!.dispatchCommand(KEY_BACKSPACE_COMMAND, event) })
      // "h" + (the selection, the whole mention, and its trailing space all
      // consumed) -> "hthere" — never a fragment like "h @src/main."
      await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent('hthere'))
    })

    it('a Backspace that touches no staged mention leaves the mention intact (handler declines)', async () => {
      const editorRef = createRef<LexicalEditor>()
      const controlRef: MutableRefObject<ComposerControl | null> = { current: null }
      const initial = `hello ${MENTION}`
      render(<ControlledHost initial={initial} editorRef={editorRef} controlRef={controlRef} onMentionKey={mentionKeyEditForSrcMain} />)
      await waitFor(() => expect(editorRef.current).not.toBeNull())
      await setCaret(controlRef, 5) // after "hello", nowhere near the mention
      const event = new KeyboardEvent('keydown', { key: 'Backspace', cancelable: true })
      act(() => { editorRef.current!.dispatchCommand(KEY_BACKSPACE_COMMAND, event) })
      // The mention handler returns null and does NOT remove the mention: a
      // delete away from any staged mention is an ordinary edit the editor's
      // own delete owns (jsdom does not simulate that native one-char delete on
      // a dispatched command, so the value is simply left untouched here).
      await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent(`hello ${MENTION}`))
    })

    it('ends the undo burst before removing the mention, so Ctrl+Z does not swallow prior typing', async () => {
      // The structural delete must get its own undo step: without ending the
      // host's typing burst, a short mention deleted within the burst window
      // merges into the current typing snapshot and one Ctrl+Z would undo both
      // the mention and the text typed just before it (Opus 5.5 / GPT review).
      const editorRef = createRef<LexicalEditor>()
      const controlRef: MutableRefObject<ComposerControl | null> = { current: null }
      const onEndUndoBurst = vi.fn()
      const initial = `hi ${MENTION} there`
      render(<ControlledHost initial={initial} editorRef={editorRef} controlRef={controlRef} onMentionKey={mentionKeyEditForSrcMain} onEndUndoBurst={onEndUndoBurst} />)
      await waitFor(() => expect(editorRef.current).not.toBeNull())
      await setCaret(controlRef, 3 + MENTION.length)
      const event = new KeyboardEvent('keydown', { key: 'Backspace', cancelable: true })
      act(() => { editorRef.current!.dispatchCommand(KEY_BACKSPACE_COMMAND, event) })
      await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent('hi there'))
      expect(onEndUndoBurst).toHaveBeenCalledTimes(1)
    })

    // Points below drive the editor with REAL keydowns on the content-editable
    // (fireEvent.keyDown), not a hand-dispatched KEY_BACKSPACE_COMMAND, so
    // Lexical 0.50.0's own keydown shortcut table runs and maps each chord to
    // the command this file now registers on: a word-delete chord (Alt/Ctrl +
    // Backspace/Delete) -> DELETE_WORD_COMMAND, a plain or forward delete ->
    // DELETE_CHARACTER_COMMAND. A test that only dispatched KEY_BACKSPACE_COMMAND
    // would skip that table and stay green even if the registration missed the
    // chord (the gap finding 1 closed).
    const editable = () => screen.getByRole('textbox')
    // Lexical's word-delete chord is Option (altKey) on Apple and Ctrl elsewhere
    // (its CONTROL_OR_ALT mask, derived from navigator.platform at import). The
    // editor's own IS_MAC reads the same platform in this process, so firing this
    // modifier matches whatever Lexical routes to DELETE_WORD_COMMAND on the
    // running platform — the Linux CI runner (Ctrl) and a mac dev box (Alt) both.
    const wordDeleteMod = IS_MAC ? { altKey: true } : { ctrlKey: true }

    it('word-delete chord just past a mention removes the whole mention (DELETE_WORD_COMMAND backward)', async () => {
      const editorRef = createRef<LexicalEditor>()
      const controlRef: MutableRefObject<ComposerControl | null> = { current: null }
      const initial = `hi ${MENTION} there`
      render(<ControlledHost initial={initial} editorRef={editorRef} controlRef={controlRef} onMentionKey={mentionKeyEditForSrcMain} />)
      await waitFor(() => expect(editorRef.current).not.toBeNull())
      await setCaret(controlRef, 3 + MENTION.length)
      // Alt+Backspace on mac / Ctrl+Backspace elsewhere -> DELETE_WORD_COMMAND(isBackward=true).
      fireEvent.keyDown(editable(), { key: 'Backspace', ...wordDeleteMod })
      await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent('hi there'))
    })

    it('word-delete chord just before a mention removes the whole mention (DELETE_WORD_COMMAND forward)', async () => {
      const editorRef = createRef<LexicalEditor>()
      const controlRef: MutableRefObject<ComposerControl | null> = { current: null }
      const initial = `hi ${MENTION} there`
      render(<ControlledHost initial={initial} editorRef={editorRef} controlRef={controlRef} onMentionKey={mentionKeyEditForSrcMain} />)
      await waitFor(() => expect(editorRef.current).not.toBeNull())
      await setCaret(controlRef, 3) // at the `@`
      // Alt+Delete on mac / Ctrl+Delete elsewhere -> DELETE_WORD_COMMAND(isBackward=false).
      fireEvent.keyDown(editable(), { key: 'Delete', ...wordDeleteMod })
      await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent('hi there'))
    })

    it('a plain Backspace keydown inside a mention removes it whole via DELETE_CHARACTER_COMMAND', async () => {
      const editorRef = createRef<LexicalEditor>()
      const controlRef: MutableRefObject<ComposerControl | null> = { current: null }
      const initial = `hi ${MENTION} there`
      render(<ControlledHost initial={initial} editorRef={editorRef} controlRef={controlRef} onMentionKey={mentionKeyEditForSrcMain} />)
      await waitFor(() => expect(editorRef.current).not.toBeNull())
      await setCaret(controlRef, 3 + MENTION.length)
      // Plain Backspace -> KEY_BACKSPACE_COMMAND -> DELETE_CHARACTER_COMMAND(true).
      fireEvent.keyDown(editable(), { key: 'Backspace' })
      await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent('hi there'))
    })

    it('a plain Delete keydown just before a mention removes it whole via DELETE_CHARACTER_COMMAND', async () => {
      const editorRef = createRef<LexicalEditor>()
      const controlRef: MutableRefObject<ComposerControl | null> = { current: null }
      const initial = `hi ${MENTION} there`
      render(<ControlledHost initial={initial} editorRef={editorRef} controlRef={controlRef} onMentionKey={mentionKeyEditForSrcMain} />)
      await waitFor(() => expect(editorRef.current).not.toBeNull())
      await setCaret(controlRef, 3) // at the `@`
      // Plain Delete -> KEY_DELETE_COMMAND -> DELETE_CHARACTER_COMMAND(false).
      fireEvent.keyDown(editable(), { key: 'Delete' })
      await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent('hi there'))
    })

    it('a plain Backspace keydown away from any mention deletes exactly one character (no over-delete)', async () => {
      const editorRef = createRef<LexicalEditor>()
      const controlRef: MutableRefObject<ComposerControl | null> = { current: null }
      // Mention first so onMentionKey is wired; the caret sits in plain text
      // far from it, so the handler declines and Lexical's own character delete
      // runs — removing exactly one character, never spilling into the mention.
      const initial = `${MENTION} hello`
      render(<ControlledHost initial={initial} editorRef={editorRef} controlRef={controlRef} onMentionKey={mentionKeyEditForSrcMain} />)
      await waitFor(() => expect(editorRef.current).not.toBeNull())
      // Caret just after the "o" of "hello" (end of value), away from the mention.
      await setCaret(controlRef, initial.length)
      fireEvent.keyDown(editable(), { key: 'Backspace' })
      // Exactly one char gone: "hello" -> "hell"; the mention is untouched.
      await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent(`${MENTION} hell`))
    })

    it('one undo restores the whole mention as a unit; a second undo then reverts the earlier typing', async () => {
      // The user-facing promise of the atomic delete + endUndoBurst: one Ctrl+Z
      // brings the mention back whole and does NOT also undo the text typed just
      // before it. endUndoBurst is wired to the host's history commit so the
      // delete lands in its own undo step.
      const editorRef = createRef<LexicalEditor>()
      const controlRef: MutableRefObject<ComposerControl | null> = { current: null }
      // Host wiring: the composer is controlled, so reproduce the real end-undo
      // seam by committing the current editor state as a discrete snapshot
      // before the structural delete, exactly as the production host does.
      const initial = `hi ${MENTION} there`
      render(<ControlledHost initial={initial} editorRef={editorRef} controlRef={controlRef} onMentionKey={mentionKeyEditForSrcMain} onEndUndoBurst={() => {}} />)
      await waitFor(() => expect(editorRef.current).not.toBeNull())
      // Type a character so there is a prior typing step for the second undo to
      // reach: append " x" at the end.
      await setCaret(controlRef, initial.length)
      await dispatchAtEnd(editorRef.current!, CONTROLLED_TEXT_INSERTION_COMMAND, ' x')
      await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent(`hi ${MENTION} there x`))
      // Delete the mention atomically with a real Backspace keydown.
      await setCaret(controlRef, 3 + MENTION.length)
      fireEvent.keyDown(editable(), { key: 'Backspace' })
      await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent('hi there x'))
      // One undo brings the whole mention back — not a single character.
      act(() => { editorRef.current!.dispatchCommand(UNDO_COMMAND, undefined) })
      await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent(`hi ${MENTION} there x`))
      // A second undo reverts the earlier typing, not the mention.
      act(() => { editorRef.current!.dispatchCommand(UNDO_COMMAND, undefined) })
      await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent(`hi ${MENTION} there`))
    })
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

  it.each([
    ['Ctrl+Shift+V', { key: 'V', code: 'KeyV', ctrlKey: true, shiftKey: true }],
    ['Cmd+Option+Shift+V', { key: '◊', code: 'KeyV', metaKey: true, altKey: true, shiftKey: true }],
  ])('keeps a %s raw paste inline and preserves trailing blanks', async (_chord, init) => {
    const editorRef = createRef<LexicalEditor>()
    render(<ControlledHost editorRef={editorRef} />)
    await waitFor(() => expect(editorRef.current).not.toBeNull())
    act(() => {
      editorRef.current!.dispatchCommand(KEY_MODIFIER_COMMAND, new KeyboardEvent('keydown', init))
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

  it.each([
    ['Cmd+Option+V', { key: '√', code: 'KeyV', metaKey: true, altKey: true }],
    ['Ctrl+Alt+Shift+V', { key: 'V', code: 'KeyV', ctrlKey: true, altKey: true, shiftKey: true }],
  ])('still collapses a large paste after %s', async (_chord, init) => {
    const editorRef = createRef<LexicalEditor>()
    render(<ControlledHost editorRef={editorRef} />)
    await waitFor(() => expect(editorRef.current).not.toBeNull())
    act(() => {
      editorRef.current!.dispatchCommand(KEY_MODIFIER_COMMAND, new KeyboardEvent('keydown', init))
    })
    const event = new Event('paste', { cancelable: true }) as ClipboardEvent
    Object.defineProperty(event, 'clipboardData', {
      value: { types: ['text/plain'], items: [], getData: () => 'one\ntwo\nthree\nfour' },
    })
    await dispatchAtEnd(editorRef.current!, PASTE_COMMAND, event)
    await waitFor(() => expect(screen.getByTestId('paste-token-1')).toBeInTheDocument())
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

  it.each(['Enter', ' '])('opens a selected token preview on %s activation', async key => {
    const editorRef = createRef<LexicalEditor>()
    const onSend = vi.fn()
    render(<ControlledHost initial={formatToken(block)} initialBlocks={[block]} editorRef={editorRef} onSend={onSend} />)
    const chip = screen.getByTestId('paste-token-1')
    chip.focus()
    fireEvent.keyDown(chip, { key })
    expect(await screen.findByTestId('lexical-paste-preview-1')).toBeInTheDocument()
    expect(chip).toHaveAttribute('aria-expanded', 'true')
    expect(chip.getAttribute('aria-label')).toContain(chip.getAttribute('title') || '')
    expect(chip.getAttribute('aria-label')).toContain('Paste #1 · 4 lines')
    // The chip's action is a PREVIEW toggle, not the transcript chip's inline
    // expand/collapse — the accessible name must say what it actually does.
    expect(chip.getAttribute('title')).toBe('Hide paste preview')
    // Chip activation must stay the chip's: the same keystroke must never
    // reach the editor's send-on-Enter handler and submit the draft.
    expect(onSend).not.toHaveBeenCalled()
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

  function pressArrow(editor: LexicalEditor, key: 'ArrowUp' | 'ArrowDown', init: KeyboardEventInit = {}): boolean {
    let handled = false
    act(() => {
      handled = editor.dispatchCommand(
        key === 'ArrowUp' ? KEY_ARROW_UP_COMMAND : KEY_ARROW_DOWN_COMMAND,
        new KeyboardEvent('keydown', { key, ...init }),
      )
    })
    return handled
  }

  it('keeps the recalled prompt when older history loads in front mid-browse', async () => {
    const editorRef = createRef<LexicalEditor>()
    const controlRef: MutableRefObject<ComposerControl | null> = { current: null }
    const { rerender } = render(
      <ControlledHost editorRef={editorRef} controlRef={controlRef} sentMessages={['first', 'second', 'third']} />,
    )
    await waitFor(() => expect(controlRef.current).not.toBeNull())
    act(() => controlRef.current!.setSelection(0))
    pressArrow(editorRef.current!, 'ArrowUp')
    await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent('third'))
    act(() => controlRef.current!.setSelection(0))
    pressArrow(editorRef.current!, 'ArrowUp')
    await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent('second'))
    rerender(
      <ControlledHost editorRef={editorRef} controlRef={controlRef} sentMessages={['zero', 'first', 'second', 'third']} />,
    )
    act(() => controlRef.current!.setSelection(0))
    pressArrow(editorRef.current!, 'ArrowUp')
    await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent(/^first$/))
  })

  it('ends browsing when the history scope changes, so another slot never gets this draft', async () => {
    const editorRef = createRef<LexicalEditor>()
    const controlRef: MutableRefObject<ComposerControl | null> = { current: null }
    const { rerender } = render(
      <ControlledHost initial="draft A" editorRef={editorRef} controlRef={controlRef} historyScope="a" sentMessages={['same']} />,
    )
    await waitFor(() => expect(controlRef.current).not.toBeNull())
    act(() => controlRef.current!.setSelection(0))
    pressArrow(editorRef.current!, 'ArrowUp')
    await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent('same'))
    rerender(
      <ControlledHost initial="draft A" editorRef={editorRef} controlRef={controlRef} historyScope="b" sentMessages={['other', 'same']} />,
    )
    act(() => controlRef.current!.setSelection('same'.length))
    expect(pressArrow(editorRef.current!, 'ArrowDown')).toBe(false)
    expect(screen.getByTestId('value')).toHaveTextContent(/^same$/)
  })

  it('moves the caret instead of recalling once a recalled prompt is edited', async () => {
    const editorRef = createRef<LexicalEditor>()
    const controlRef: MutableRefObject<ComposerControl | null> = { current: null }
    render(
      <ControlledHost initial="draft" editorRef={editorRef} controlRef={controlRef} sentMessages={['first', 'second']} />,
    )
    await waitFor(() => expect(controlRef.current).not.toBeNull())
    act(() => controlRef.current!.setSelection(0))
    pressArrow(editorRef.current!, 'ArrowUp')
    await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent('second'))
    await dispatchAtEnd(editorRef.current!, CONTROLLED_TEXT_INSERTION_COMMAND, '!')
    await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent('second!'))
    act(() => controlRef.current!.setSelection('second!'.length))
    expect(pressArrow(editorRef.current!, 'ArrowDown')).toBe(false)
    expect(screen.getByTestId('value')).toHaveTextContent('second!')
  })

  it('ignores an auto-repeat ↑ that reaches the start of a typed draft', async () => {
    const editorRef = createRef<LexicalEditor>()
    const controlRef: MutableRefObject<ComposerControl | null> = { current: null }
    render(
      <ControlledHost initial="draft" editorRef={editorRef} controlRef={controlRef} sentMessages={['first', 'second']} />,
    )
    await waitFor(() => expect(controlRef.current).not.toBeNull())
    act(() => controlRef.current!.setSelection(0))
    expect(pressArrow(editorRef.current!, 'ArrowUp', { repeat: true })).toBe(false)
    expect(screen.getByTestId('value')).toHaveTextContent(/^draft$/)
    // Letting go and pressing again is a deliberate recall.
    pressArrow(editorRef.current!, 'ArrowUp')
    await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent('second'))
  })

  it('keeps walking history on auto-repeat once a prompt is recalled', async () => {
    const editorRef = createRef<LexicalEditor>()
    const controlRef: MutableRefObject<ComposerControl | null> = { current: null }
    render(
      <ControlledHost initial="draft" editorRef={editorRef} controlRef={controlRef} sentMessages={['first', 'second']} />,
    )
    await waitFor(() => expect(controlRef.current).not.toBeNull())
    act(() => controlRef.current!.setSelection(0))
    pressArrow(editorRef.current!, 'ArrowUp')
    await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent('second'))
    act(() => controlRef.current!.setSelection(0))
    pressArrow(editorRef.current!, 'ArrowUp', { repeat: true })
    await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent(/^first$/))
  })

  it('recalls on an auto-repeat ↑ from an empty composer', async () => {
    const editorRef = createRef<LexicalEditor>()
    const controlRef: MutableRefObject<ComposerControl | null> = { current: null }
    render(
      <ControlledHost initial="" editorRef={editorRef} controlRef={controlRef} sentMessages={['first', 'second']} />,
    )
    await waitFor(() => expect(controlRef.current).not.toBeNull())
    act(() => controlRef.current!.setSelection(0))
    pressArrow(editorRef.current!, 'ArrowUp', { repeat: true })
    await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent('second'))
  })

  it('fires the edit-last-message request on ⌘↑/Ctrl+↑ from an empty composer (#11402)', async () => {
    const editorRef = createRef<LexicalEditor>()
    const controlRef: MutableRefObject<ComposerControl | null> = { current: null }
    const onEditLastRequest = vi.fn()
    render(
      <ControlledHost
        initial=""
        editorRef={editorRef}
        controlRef={controlRef}
        sentMessages={['first', 'second']}
        onEditLastRequest={onEditLastRequest}
      />,
    )
    await waitFor(() => expect(controlRef.current).not.toBeNull())
    act(() => {
      editorRef.current!.dispatchCommand(
        KEY_ARROW_UP_COMMAND,
        new KeyboardEvent('keydown', { key: 'ArrowUp', ctrlKey: true, cancelable: true }),
      )
    })
    await waitFor(() => expect(onEditLastRequest).toHaveBeenCalledTimes(1))
    // The chord is claimed, so the plain-↑ history recall must not have run.
    await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent(''))
  })

  it('does not fire the edit-last-message request when the composer has content', async () => {
    const editorRef = createRef<LexicalEditor>()
    const controlRef: MutableRefObject<ComposerControl | null> = { current: null }
    const onEditLastRequest = vi.fn()
    render(
      <ControlledHost
        initial="draft text"
        editorRef={editorRef}
        controlRef={controlRef}
        sentMessages={['first', 'second']}
        onEditLastRequest={onEditLastRequest}
      />,
    )
    await waitFor(() => expect(controlRef.current).not.toBeNull())
    act(() => {
      editorRef.current!.dispatchCommand(
        KEY_ARROW_UP_COMMAND,
        new KeyboardEvent('keydown', { key: 'ArrowUp', ctrlKey: true, cancelable: true }),
      )
    })
    expect(onEditLastRequest).not.toHaveBeenCalled()
    // Unclaimed: with content the chord must do nothing (not even recall).
    await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent('draft text'))
  })

  it('leaves plain ↑ history recall untouched alongside the ⌘↑ binding', async () => {
    const editorRef = createRef<LexicalEditor>()
    const controlRef: MutableRefObject<ComposerControl | null> = { current: null }
    const onEditLastRequest = vi.fn()
    render(
      <ControlledHost
        initial=""
        editorRef={editorRef}
        controlRef={controlRef}
        sentMessages={['first', 'second']}
        onEditLastRequest={onEditLastRequest}
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
    expect(onEditLastRequest).not.toHaveBeenCalled()
  })

})
