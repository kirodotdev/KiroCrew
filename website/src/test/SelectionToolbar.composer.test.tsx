/**
 * SelectionToolbar in composer mode — the file viewer's type-first annotator.
 *
 * Selecting text opens a focused comment input straight away; the host's
 * actions become icon buttons beside it (below it on a narrow viewport). The
 * scenes here pin the keyboard contract (Enter / Shift+Enter / Escape / Cmd+C),
 * the focus-first behaviour, the open/submit/close handshake with the host, and
 * the width-dependent layout. Chat's toolbar passes no composer and is covered
 * by the plain-row scene at the end.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { useRef } from 'react'
import SelectionToolbar, {
  rangeFromTextOffset,
  type SelectionAction,
  type SelectionComposer,
} from '../components/SelectionToolbar'

/** The composer stacks its controls under the input below this viewport width. */
const STACK_BELOW_PX = 480

// Skip animations so hide/show is synchronous; motion.div forwards its ref so
// the clamp layout effect can measure the box.
vi.mock('framer-motion', async () => {
  const { forwardRef } = await import('react')
  return {
    AnimatePresence: ({ children }: { children: React.ReactNode }) => children,
    motion: {
      div: forwardRef(({ children, ...props }: Record<string, unknown> & { children?: React.ReactNode }, ref: React.Ref<HTMLDivElement>) => {
        // framer-only props must not reach the DOM node.
        const { initial: _i, animate: _a, exit: _e, transition: _t, ...rest } = props as Record<string, unknown>
        return <div ref={ref} {...(rest as React.HTMLAttributes<HTMLDivElement>)}>{children}</div>
      }),
    },
  }
})

if (!Range.prototype.getBoundingClientRect) {
  Range.prototype.getBoundingClientRect = function () { return new DOMRect(10, 10, 100, 20) }
}

// The Cmd/Ctrl+C shortcut copies through the shared clipboard helper; its
// boolean result decides between the checkmark and the failure notice.
const copyToClipboardMock = vi.fn<(text: string) => Promise<boolean>>()
vi.mock('../utils/clipboard', () => ({ copyToClipboard: (text: string) => copyToClipboardMock(text) }))

// Touch gates the auto-focus off; scenes flip it explicitly.
const touchEnv = { touch: false }
vi.mock('../utils/isTouchDevice', () => ({ isTouchDevice: () => touchEnv.touch }))

const TEXT = 'alpha beta gamma'

function selectInContainer(container: HTMLElement, word: string) {
  const textNode = container.firstChild as Text
  const start = textNode.data.indexOf(word)
  const range = document.createRange()
  range.setStart(textNode, start)
  range.setEnd(textNode, start + word.length)
  const sel = window.getSelection()!
  sel.removeAllRanges()
  sel.addRange(range)
}

function Harness({ actions, composer, suspended }: { actions: SelectionAction[]; composer?: SelectionComposer; suspended?: boolean }) {
  const ref = useRef<HTMLDivElement>(null)
  return (
    <div>
      <div ref={ref} data-testid="container">{TEXT}</div>
      <SelectionToolbar containerRef={ref} actions={actions} composer={composer} suspended={suspended} />
    </div>
  )
}

/** Select `word`, release the mouse, and wait for the composer input. */
async function openComposer(word = 'beta') {
  selectInContainer(screen.getByTestId('container'), word)
  fireEvent.mouseUp(document, { clientX: 100, clientY: 50 })
  return screen.findByLabelText('Comment on the selected text')
}

const composerBox = () => screen.getByTestId('selection-composer')

let onCopy: ReturnType<typeof vi.fn>
let actions: SelectionAction[]
let composer: SelectionComposer & { onOpen: ReturnType<typeof vi.fn>; onSubmit: ReturnType<typeof vi.fn>; onClose: ReturnType<typeof vi.fn> }
let originalInnerWidth: number

beforeEach(() => {
  onCopy = vi.fn()
  actions = [{ id: 'copy', icon: <span data-testid="copy-icon" />, label: 'Copy', hint: 'Copies the selection', onClick: onCopy }]
  copyToClipboardMock.mockReset()
  copyToClipboardMock.mockResolvedValue(true)
  composer = {
    onOpen: vi.fn(),
    onSubmit: vi.fn(),
    onClose: vi.fn(),
  }
  originalInnerWidth = window.innerWidth
  Object.defineProperty(window, 'innerWidth', { value: 1024, configurable: true, writable: true })
})

afterEach(() => {
  Object.defineProperty(window, 'innerWidth', { value: originalInnerWidth, configurable: true, writable: true })
  window.getSelection()?.removeAllRanges()
})

describe('SelectionToolbar — composer mode', () => {
  it('opens a focused comment input as soon as text is selected, no button first', async () => {
    // Record what had focus when the host was told about the open: the host
    // resolves the anchor from the live selection, which focus collapses, so
    // the input must NOT be focused yet at that moment.
    let activeAtOpen: string | undefined
    composer.onOpen = vi.fn(() => { activeAtOpen = document.activeElement?.tagName })
    render(<Harness actions={actions} composer={composer} />)
    const input = await openComposer()

    expect(input).toHaveAttribute('placeholder', 'Write a comment…')
    await waitFor(() => expect(input).toHaveFocus())
    expect(composer.onOpen).toHaveBeenCalledWith('beta')
    expect(activeAtOpen).not.toBe('TEXTAREA')
    // No "Comment" button anywhere — the input IS the comment affordance.
    expect(screen.queryByRole('button', { name: 'Comment' })).toBeNull()
  })

  it('shows a labelled submit button, then the host actions and a close button past a divider', async () => {
    render(<Harness actions={actions} composer={composer} />)
    await openComposer()

    // Submit carries a visible verb, not just an icon; disabled until there is text.
    const submit = screen.getByRole('button', { name: 'Add comment' })
    expect(submit).toHaveTextContent('Add comment')
    expect(submit).toBeDisabled()

    const copy = screen.getByRole('button', { name: 'Copy' })
    // The hint says what Copy copies — the selection, not the draft — as tooltip
    // AND accessible description.
    expect(copy).toHaveAttribute('title', 'Copies the selection')
    expect(copy).toHaveAccessibleDescription('Copies the selection')
    // Icon AND verb: an unlabelled glyph beside a comment box reads as "copy the comment".
    expect(copy).toHaveTextContent('Copy')
    expect(copy.querySelector('[data-testid="copy-icon"]')).not.toBeNull()
    const group = screen.getByRole('group', { name: 'More actions for the selection' })
    expect(group).toContainElement(copy)
    // The selection-scoped group (Copy, Close) is separate from the submit.
    expect(group).not.toContainElement(submit)
    expect(group).toContainElement(screen.getByRole('button', { name: 'Close' }))
  })

  it('the close button closes without a submit and hands the selection back', async () => {
    render(<Harness actions={actions} composer={composer} />)
    const input = await openComposer()
    fireEvent.change(input, { target: { value: 'abandoned' } })
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.queryByTestId('selection-composer')).toBeNull())
    expect(composer.onClose).toHaveBeenCalledTimes(1)
    expect(composer.onSubmit).not.toHaveBeenCalled()
    expect(window.getSelection()?.toString()).toBe('beta')
  })

  it('the icon actions work with an empty input — a Copy-only user loses nothing', async () => {
    render(<Harness actions={actions} composer={composer} />)
    const input = await openComposer()

    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    expect(onCopy).toHaveBeenCalledWith('beta', expect.any(DOMRect))
    // Copy does not dismiss the box (matches the plain toolbar) and the input keeps focus.
    expect(input).toBeInTheDocument()
    expect(composer.onClose).not.toHaveBeenCalled()
  })

  it('a multi-range (Firefox ctrl+drag) selection anchors the composer to the first range only', async () => {
    render(<Harness actions={actions} composer={composer} />)
    const container = screen.getByTestId('container')
    const textNode = container.firstChild as Text
    const mk = (word: string) => {
      const r = document.createRange()
      r.setStart(textNode, TEXT.indexOf(word)); r.setEnd(textNode, TEXT.indexOf(word) + word.length)
      return r
    }
    const ranges = [mk('beta'), mk('gamma')]
    const sel = window.getSelection()!
    sel.removeAllRanges(); sel.addRange(ranges[0])
    // Only Gecko exposes more than one range; shape the Selection the way it does.
    Object.defineProperty(sel, 'rangeCount', { configurable: true, get: () => ranges.length })
    Object.defineProperty(sel, 'getRangeAt', { configurable: true, value: (i: number) => ranges[i] })
    Object.defineProperty(sel, 'toString', { configurable: true, value: () => 'beta gamma' })
    try {
      fireEvent.mouseUp(document, { clientX: 100, clientY: 50 })
      await screen.findByLabelText('Comment on the selected text')
      // Text and offsets describe the same span: the anchor is "beta", not the
      // aggregated "beta gamma" the offsets (taken from range 0) do not cover.
      expect(composer.onOpen).toHaveBeenCalledWith('beta')
    } finally {
      delete (sel as unknown as Record<string, unknown>).rangeCount
      delete (sel as unknown as Record<string, unknown>).getRangeAt
      delete (sel as unknown as Record<string, unknown>).toString
    }
  })

  it('a second click on submit inside the exit window does not append the comment twice', async () => {
    render(<Harness actions={actions} composer={composer} />)
    const input = await openComposer()
    fireEvent.change(input, { target: { value: 'once' } })
    const submit = screen.getByRole('button', { name: 'Add comment' })
    // The exiting box is still rendered with its last props for the length of
    // the exit animation, so a double-click lands both clicks on a live button.
    fireEvent.click(submit)
    fireEvent.click(submit)
    expect(composer.onSubmit).toHaveBeenCalledTimes(1)
    expect(composer.onSubmit).toHaveBeenCalledWith('once', 'beta')
  })

  it('Enter submits the comment together with the selection; Shift+Enter does not', async () => {
    render(<Harness actions={actions} composer={composer} />)
    const input = await openComposer()

    fireEvent.change(input, { target: { value: 'needs a source' } })
    fireEvent.keyDown(input, { key: 'Enter', shiftKey: true })
    expect(composer.onSubmit).not.toHaveBeenCalled()
    expect(input).toBeInTheDocument()

    fireEvent.keyDown(input, { key: 'Enter' })
    expect(composer.onSubmit).toHaveBeenCalledTimes(1)
    expect(composer.onSubmit).toHaveBeenCalledWith('needs a source', 'beta')
    // Submit and close are exclusive: the host is not ALSO told it closed.
    await waitFor(() => expect(screen.queryByLabelText('Comment on the selected text')).toBeNull())
    expect(composer.onClose).not.toHaveBeenCalled()
    // A submitted selection is released — the host's <mark>s stand in for it now.
    expect(window.getSelection()?.toString()).toBe('')
  })

  it('an empty Enter neither submits nor closes', async () => {
    render(<Harness actions={actions} composer={composer} />)
    const input = await openComposer()
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(composer.onSubmit).not.toHaveBeenCalled()
    expect(input).toBeInTheDocument()
  })

  it('the Add comment button submits the typed text', async () => {
    render(<Harness actions={actions} composer={composer} />)
    const input = await openComposer()
    fireEvent.change(input, { target: { value: 'tighten this' } })
    const submit = screen.getByRole('button', { name: 'Add comment' })
    expect(submit).toBeEnabled()
    fireEvent.click(submit)
    expect(composer.onSubmit).toHaveBeenCalledWith('tighten this', 'beta')
  })

  it('Escape closes the box, tells the host once, and hands the text selection back', async () => {
    render(<Harness actions={actions} composer={composer} />)
    const input = await openComposer()
    fireEvent.change(input, { target: { value: 'half a thou' } })

    fireEvent.keyDown(input, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByLabelText('Comment on the selected text')).toBeNull())
    expect(composer.onClose).toHaveBeenCalledTimes(1)
    expect(composer.onSubmit).not.toHaveBeenCalled()
    // The selection focus stole is restored from its text offsets.
    expect(window.getSelection()?.toString()).toBe('beta')
  })

  it('Escape over a typed draft asks the host first; declining keeps the draft and refocuses', async () => {
    const confirmDiscard = vi.fn<() => Promise<boolean>>().mockResolvedValue(false)
    render(<Harness actions={actions} composer={{ ...composer, confirmDiscard }} />)
    const input = await openComposer()
    fireEvent.change(input, { target: { value: 'multi-line effort' } })
    fireEvent.keyDown(input, { key: 'Escape' })
    await waitFor(() => expect(confirmDiscard).toHaveBeenCalledTimes(1))
    await act(async () => { await new Promise(r => setTimeout(r, 20)) })
    expect(screen.getByTestId('selection-composer')).toBeInTheDocument()
    expect(input).toHaveValue('multi-line effort')
    expect(composer.onClose).not.toHaveBeenCalled()
    // Accepting discards and closes.
    confirmDiscard.mockResolvedValue(true)
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.queryByTestId('selection-composer')).toBeNull())
    expect(composer.onClose).toHaveBeenCalledTimes(1)
  })

  it('Escape over an EMPTY input never asks', async () => {
    const confirmDiscard = vi.fn<() => Promise<boolean>>().mockResolvedValue(false)
    render(<Harness actions={actions} composer={{ ...composer, confirmDiscard }} />)
    const input = await openComposer()
    fireEvent.keyDown(input, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('selection-composer')).toBeNull())
    expect(confirmDiscard).not.toHaveBeenCalled()
  })

  it('Escape on an icon button (keyboard user tabbed out of the input) also closes', async () => {
    render(<Harness actions={actions} composer={composer} />)
    await openComposer()
    const copy = screen.getByRole('button', { name: 'Copy' })
    copy.focus()
    fireEvent.keyDown(copy, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('selection-composer')).toBeNull())
    expect(composer.onClose).toHaveBeenCalledTimes(1)
  })

  it('Cmd/Ctrl+C in an empty input copies the SELECTED document text, not the empty box', async () => {
    render(<Harness actions={actions} composer={composer} />)
    const input = await openComposer()

    fireEvent.keyDown(input, { key: 'c', metaKey: true })
    await waitFor(() => expect(copyToClipboardMock).toHaveBeenCalledWith('beta'))
    fireEvent.keyDown(input, { key: 'c', ctrlKey: true })
    await waitFor(() => expect(copyToClipboardMock).toHaveBeenCalledTimes(2))
    // Success shows the checkmark on the Copy action and nothing else.
    expect(screen.queryByText(/^Copy failed/)).toBeNull()
    // Still open: copying is not a close.
    expect(input).toBeInTheDocument()
    expect(composer.onClose).not.toHaveBeenCalled()
  })

  it('Cmd/Ctrl+C with a draft is the textarea\u2019s own shortcut and never copies the document text', async () => {
    render(<Harness actions={actions} composer={composer} />)
    const input = await openComposer()
    fireEvent.change(input, { target: { value: 'draft' } })
    fireEvent.keyDown(input, { key: 'c', metaKey: true })
    await act(async () => { await new Promise(r => setTimeout(r, 20)) })
    expect(copyToClipboardMock).not.toHaveBeenCalled()
  })

  it('reports a refused clipboard write instead of showing a success checkmark', async () => {
    copyToClipboardMock.mockResolvedValue(false)
    render(<Harness actions={actions} composer={composer} />)
    const input = await openComposer()
    fireEvent.keyDown(input, { key: 'c', metaKey: true })
    expect(await screen.findByText(/^Copy failed/)).toBeInTheDocument()
    // No checkmark swapped into the Copy button.
    expect(screen.getByRole('button', { name: 'Copy' }).querySelector('[data-testid="copy-icon"]')).not.toBeNull()
  })

  it('the Copy BUTTON is result-aware when the host action returns the clipboard promise', async () => {
    const resultAware: SelectionAction[] = [{ id: 'copy', icon: <span data-testid="copy-icon" />, label: 'Copy', onClick: () => Promise.resolve(false) }]
    render(<Harness actions={resultAware} composer={composer} />)
    await openComposer()
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    expect(await screen.findByText(/^Copy failed/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Copy' }).querySelector('[data-testid="copy-icon"]')).not.toBeNull()
    // Dismissable, and a later success clears it and shows the checkmark.
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByText(/^Copy failed/)).toBeNull()
  })

  it('tells the host when a draft appears and when it is gone', async () => {
    const onDraftChange = vi.fn()
    render(<Harness actions={actions} composer={{ ...composer, onDraftChange }} />)
    const input = await openComposer()
    fireEvent.change(input, { target: { value: 'd' } })
    expect(onDraftChange).toHaveBeenLastCalledWith(true, { anchor: 'beta', start: 6 })
    fireEvent.change(input, { target: { value: '   ' } })
    expect(onDraftChange).toHaveBeenLastCalledWith(false, null)
    fireEvent.change(input, { target: { value: 'draft' } })
    expect(onDraftChange).toHaveBeenLastCalledWith(true, { anchor: 'beta', start: 6 })
    // Submit clears the draft flag for the host too.
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onDraftChange).toHaveBeenLastCalledWith(false, null)
  })

  it('opens UNFOCUSED for a keyboard (Shift+Arrow) selection so the next arrow still extends it; Enter moves in', async () => {
    render(<Harness actions={actions} composer={composer} />)
    selectInContainer(screen.getByTestId('container'), 'beta')
    fireEvent.keyUp(document, { key: 'ArrowRight', shiftKey: true })
    const input = await screen.findByLabelText('Comment on the selected text')
    await act(async () => { await new Promise(r => setTimeout(r, 40)) })
    expect(input).not.toHaveFocus()
    expect(input).toHaveAttribute('placeholder', 'Press Enter or click to comment…')
    // The document selection is still the user's.
    expect(window.getSelection()?.toString()).toBe('beta')
    // A bare Enter from the document moves the caret into the box — and the
    // placeholder stops saying "Press Enter" once the caret is here.
    fireEvent.keyDown(document.body, { key: 'Enter' })
    expect(input).toHaveFocus()
    fireEvent.focus(input)
    expect(input).toHaveAttribute('placeholder', 'Write a comment…')
  })

  it('opens unfocused on a touch device (no keyboard pop-up per selection)', async () => {
    touchEnv.touch = true
    try {
      render(<Harness actions={actions} composer={composer} />)
      const input = await openComposer()
      await act(async () => { await new Promise(r => setTimeout(r, 40)) })
      expect(input).not.toHaveFocus()
    } finally {
      touchEnv.touch = false
    }
  })

  it('a click away with a typed draft keeps the box open instead of discarding it', async () => {
    render(<Harness actions={actions} composer={composer} />)
    const input = await openComposer()
    fireEvent.change(input, { target: { value: 'half written' } })
    fireEvent.mouseDown(document.body)
    await act(async () => { await new Promise(r => setTimeout(r, 40)) })
    expect(screen.getByTestId('selection-composer')).toBeInTheDocument()
    expect(composer.onClose).not.toHaveBeenCalled()
    // Clearing the draft restores the ordinary click-away.
    fireEvent.change(input, { target: { value: '' } })
    fireEvent.mouseDown(document.body)
    await waitFor(() => expect(screen.queryByTestId('selection-composer')).toBeNull())
    expect(composer.onClose).toHaveBeenCalledTimes(1)
  })

  it('typing a shifted character into the input does not re-run the selection check and close it', async () => {
    render(<Harness actions={actions} composer={composer} />)
    const input = await openComposer()
    // Focus collapsed the document selection; a Shift+key keyup at document
    // level used to re-check it and hide the toolbar.
    window.getSelection()?.removeAllRanges()
    fireEvent.keyUp(input, { key: 'A', shiftKey: true })
    await act(async () => { await new Promise(r => setTimeout(r, 80)) })
    expect(screen.getByLabelText('Comment on the selected text')).toBeInTheDocument()
    expect(composer.onClose).not.toHaveBeenCalled()
  })

  it('a click away closes the box and tells the host exactly once', async () => {
    render(<Harness actions={actions} composer={composer} />)
    await openComposer()
    fireEvent.mouseDown(document.body)
    await waitFor(() => expect(screen.queryByTestId('selection-composer')).toBeNull())
    expect(composer.onClose).toHaveBeenCalledTimes(1)
    expect(composer.onSubmit).not.toHaveBeenCalled()
  })

  it('re-selecting while open re-opens for the host with the new text', async () => {
    render(<Harness actions={actions} composer={composer} />)
    await openComposer('beta')
    selectInContainer(screen.getByTestId('container'), 'gamma')
    fireEvent.mouseUp(document, { clientX: 120, clientY: 50 })
    await waitFor(() => expect(composer.onOpen).toHaveBeenCalledTimes(2))
    expect(composer.onOpen).toHaveBeenLastCalledWith('gamma')
    // Re-opening is not a close.
    expect(composer.onClose).not.toHaveBeenCalled()
  })

  it('lays the icons out beside the input on a wide viewport and below it on a narrow one', async () => {
    render(<Harness actions={actions} composer={composer} />)
    await openComposer()
    expect(composerBox()).toHaveAttribute('data-layout', 'row')

    Object.defineProperty(window, 'innerWidth', { value: STACK_BELOW_PX - 1, configurable: true, writable: true })
    fireEvent(window, new Event('resize'))
    await waitFor(() => expect(composerBox()).toHaveAttribute('data-layout', 'stack'))
    // Stacked: the input spans the full width and the icon row sits after it.
    const input = screen.getByLabelText('Comment on the selected text')
    const group = screen.getByRole('group', { name: 'More actions for the selection' })
    expect(input.className).toContain('w-full')
    expect(input.compareDocumentPosition(group) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()

    Object.defineProperty(window, 'innerWidth', { value: 1024, configurable: true, writable: true })
    fireEvent(window, new Event('resize'))
    await waitFor(() => expect(composerBox()).toHaveAttribute('data-layout', 'row'))
  })

  it('opens stacked when the viewport is already narrow', async () => {
    Object.defineProperty(window, 'innerWidth', { value: 375, configurable: true, writable: true })
    render(<Harness actions={actions} composer={composer} />)
    await openComposer()
    expect(composerBox()).toHaveAttribute('data-layout', 'stack')
  })

  it('unmounting an open composer tells the host it closed and that the draft is gone', async () => {
    const onDraftChange = vi.fn()
    const { unmount } = render(<Harness actions={actions} composer={{ ...composer, onDraftChange }} />)
    const input = await openComposer()
    fireEvent.change(input, { target: { value: 'draft' } })
    expect(onDraftChange).toHaveBeenLastCalledWith(true, { anchor: 'beta', start: 6 })
    unmount()
    expect(composer.onClose).toHaveBeenCalledTimes(1)
    // Otherwise the host's "not clean" flag would outlive the box.
    expect(onDraftChange).toHaveBeenLastCalledWith(false, null)
  })

  it('a clipboard write that REJECTS (execCommand fallback threw) is reported like a refusal', async () => {
    copyToClipboardMock.mockRejectedValue(new Error('execCommand failed'))
    render(<Harness actions={actions} composer={composer} />)
    const input = await openComposer()
    fireEvent.keyDown(input, { key: 'c', metaKey: true })
    expect(await screen.findByText(/^Copy failed/)).toBeInTheDocument()
  })

  it('a "Copy failed" notice does not survive a close to greet the next selection', async () => {
    copyToClipboardMock.mockResolvedValue(false)
    render(<Harness actions={actions} composer={composer} />)
    const input = await openComposer('beta')
    fireEvent.keyDown(input, { key: 'c', metaKey: true })
    expect(await screen.findByText(/^Copy failed/)).toBeInTheDocument()
    fireEvent.keyDown(input, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('selection-composer')).toBeNull())
    await openComposer('gamma')
    expect(screen.queryByText(/^Copy failed/)).toBeNull()
  })

  it('a Copy BUTTON whose action rejects is reported the same way, with no checkmark', async () => {
    const rejecting: SelectionAction[] = [{ id: 'copy', icon: <span data-testid="copy-icon" />, label: 'Copy', onClick: () => Promise.reject(new Error('nope')) }]
    render(<Harness actions={rejecting} composer={composer} />)
    await openComposer()
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    expect(await screen.findByText(/^Copy failed/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Copy' }).querySelector('[data-testid="copy-icon"]')).not.toBeNull()
  })
})

describe('SelectionToolbar — composer mode, drafts and hidden tabs', () => {
  it('does NOT re-anchor onto a new selection while a draft is open', async () => {
    render(<Harness actions={actions} composer={composer} />)
    const input = await openComposer('beta')
    fireEvent.change(input, { target: { value: 'about beta' } })
    selectInContainer(screen.getByTestId('container'), 'gamma')
    fireEvent.mouseUp(document, { clientX: 120, clientY: 50 })
    await act(async () => { await new Promise(r => setTimeout(r, 80)) })
    // Still one open, still anchored to the first selection.
    expect(composer.onOpen).toHaveBeenCalledTimes(1)
    expect(composer.onOpen).toHaveBeenLastCalledWith('beta')
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(composer.onSubmit).toHaveBeenCalledWith('about beta', 'beta')
  })

  it('an Escape the IME owns (candidate-list cancel) does not close the box', async () => {
    render(<Harness actions={actions} composer={composer} />)
    const input = await openComposer()
    fireEvent.change(input, { target: { value: 'にほ' } })
    fireEvent.compositionStart(input)
    fireEvent.keyDown(input, { key: 'Escape', isComposing: true })
    expect(screen.getByTestId('selection-composer')).toBeInTheDocument()
    expect(composer.onClose).not.toHaveBeenCalled()
    fireEvent.compositionEnd(input)
  })

  it('Enter on a control the user Tabbed to keeps its own activation (no focus theft)', async () => {
    render(
      <div>
        <button type="button" data-testid="outside">Submit All</button>
        <Harness actions={actions} composer={composer} />
      </div>,
    )
    selectInContainer(screen.getByTestId('container'), 'beta')
    fireEvent.keyUp(document, { key: 'ArrowRight', shiftKey: true })
    const input = await screen.findByLabelText('Comment on the selected text')
    const outside = screen.getByTestId('outside')
    outside.focus()
    fireEvent.keyDown(outside, { key: 'Enter' })
    expect(input).not.toHaveFocus()
    expect(outside).toHaveFocus()
  })

  it('suspending hides the box but keeps the draft; resuming brings both back', async () => {
    const { rerender } = render(<Harness actions={actions} composer={composer} />)
    const input = await openComposer()
    fireEvent.change(input, { target: { value: 'kept across a tab switch' } })
    rerender(<Harness actions={actions} composer={composer} suspended />)
    await waitFor(() => expect(screen.queryByTestId('selection-composer')).toBeNull())
    // Hidden, not closed: the host is not told, the draft is not gone.
    expect(composer.onClose).not.toHaveBeenCalled()
    // While hidden, selections elsewhere are ignored.
    selectInContainer(screen.getByTestId('container'), 'gamma')
    fireEvent.mouseUp(document, { clientX: 120, clientY: 50 })
    await act(async () => { await new Promise(r => setTimeout(r, 80)) })
    expect(composer.onOpen).toHaveBeenCalledTimes(1)
    rerender(<Harness actions={actions} composer={composer} suspended={false} />)
    expect(await screen.findByLabelText('Comment on the selected text')).toHaveValue('kept across a tab switch')
  })

  it('sits above the selection when there is room, and below it near the top edge', async () => {
    const rangeRect = Object.getOwnPropertyDescriptor(Range.prototype, 'getBoundingClientRect')
    const elHeight = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetHeight')
    let top = 300
    Object.defineProperty(Range.prototype, 'getBoundingClientRect', {
      configurable: true,
      value: () => ({ left: 40, right: 80, top, bottom: top + 20, width: 40, height: 20, x: 40, y: top, toJSON: () => ({}) }),
    })
    Object.defineProperty(HTMLElement.prototype, 'offsetHeight', { configurable: true, get: () => 60 })
    try {
      const { unmount } = render(<Harness actions={actions} composer={composer} />)
      await openComposer()
      const positioned = () => composerBox().parentElement as HTMLElement
      // Above: the box (60px) ends 8px over the selection's top, so the lines
      // after the selected claim stay readable.
      await waitFor(() => expect(positioned().style.top).toBe('232px'))
      unmount()

      // Too close to the top edge for the box to fit above: it hangs below instead.
      top = 20
      render(<Harness actions={actions} composer={composer} />)
      await openComposer()
      await waitFor(() => expect(positioned().style.top).toBe('48px'))
    } finally {
      if (rangeRect) Object.defineProperty(Range.prototype, 'getBoundingClientRect', rangeRect)
      else delete (Range.prototype as unknown as Record<string, unknown>).getBoundingClientRect
      if (elHeight) Object.defineProperty(HTMLElement.prototype, 'offsetHeight', elHeight)
      else delete (HTMLElement.prototype as unknown as Record<string, unknown>).offsetHeight
    }
  })

  it('resuming after a resize while hidden re-clamps the box into the new viewport', async () => {
    // happy-dom measures nothing, so give the anchor a right-hand position and
    // the box a width; the clamp is then observable through `style.left`.
    const rangeRect = Object.getOwnPropertyDescriptor(Range.prototype, 'getBoundingClientRect')
    const elWidth = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetWidth')
    Object.defineProperty(Range.prototype, 'getBoundingClientRect', {
      configurable: true,
      value: () => ({ left: 900, right: 940, top: 100, bottom: 120, width: 40, height: 20, x: 900, y: 100, toJSON: () => ({}) }),
    })
    Object.defineProperty(HTMLElement.prototype, 'offsetWidth', { configurable: true, get: () => 400 })
    try {
      const { rerender } = render(<Harness actions={actions} composer={composer} />)
      await openComposer()
      const positioned = () => composerBox().parentElement as HTMLElement
      // 1024 wide: the 400px box is pulled left so it ends 8px inside the edge.
      await waitFor(() => expect(positioned().style.left).toBe('616px'))

      rerender(<Harness actions={actions} composer={composer} suspended />)
      await waitFor(() => expect(screen.queryByTestId('selection-composer')).toBeNull())
      // The viewport narrows while the tab is hidden: nothing is rendered to re-measure.
      Object.defineProperty(window, 'innerWidth', { value: 500, configurable: true, writable: true })
      fireEvent(window, new Event('resize'))

      rerender(<Harness actions={actions} composer={composer} suspended={false} />)
      await screen.findByLabelText('Comment on the selected text')
      // Re-clamped against the 500px viewport, not left at the 1024px position.
      await waitFor(() => expect(positioned().style.left).toBe('92px'))
    } finally {
      if (rangeRect) Object.defineProperty(Range.prototype, 'getBoundingClientRect', rangeRect)
      else delete (Range.prototype as unknown as Record<string, unknown>).getBoundingClientRect
      if (elWidth) Object.defineProperty(HTMLElement.prototype, 'offsetWidth', elWidth)
      else delete (HTMLElement.prototype as unknown as Record<string, unknown>).offsetWidth
    }
  })

  /** A per-passage store double: one slot per (offset|text). */
  function makeStore() {
    const slots = new Map<string, string>()
    const k = (anchor: string, start: number) => `${start}|${anchor}`
    return {
      slots,
      read: vi.fn((anchor: string, start: number) => slots.get(k(anchor, start)) ?? null),
      write: vi.fn((text: string, anchor: string, start: number) => { slots.set(k(anchor, start), text) }),
      clear: vi.fn((anchor: string, start: number) => { slots.delete(k(anchor, start)) }),
    }
  }

  it('writes the draft (with its passage) through to the host store, and restores it on the next open OVER THAT PASSAGE after an unguarded teardown', async () => {
    const draftStore = makeStore()
    const { unmount } = render(<Harness actions={actions} composer={{ ...composer, draftStore }} />)
    const input = await openComposer('beta')
    fireEvent.change(input, { target: { value: 'survives a slot switch' } })
    // "alpha beta gamma": beta starts at offset 6 — the position rides along.
    expect(draftStore.write).toHaveBeenLastCalledWith('survives a slot switch', 'beta', 6)
    // A teardown the toolbar cannot guard (the whole panel is replaced).
    unmount()
    expect(draftStore.clear).not.toHaveBeenCalled()
    expect(draftStore.slots.get('6|beta')).toBe('survives a slot switch')

    // A DIFFERENT passage does not inherit the draft — a comment about "beta"
    // must not be filed against "gamma" — and opening, typing and closing
    // there leaves beta's draft untouched (its own slot).
    const second = render(<Harness actions={actions} composer={{ ...composer, draftStore }} />)
    const other = await openComposer('gamma')
    expect(other).toHaveValue('')
    fireEvent.change(other, { target: { value: 'about gamma' } })
    expect(draftStore.slots.get('6|beta')).toBe('survives a slot switch')
    expect(draftStore.slots.get('11|gamma')).toBe('about gamma')
    fireEvent.change(other, { target: { value: '' } })
    fireEvent.keyDown(other, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('selection-composer')).toBeNull())
    expect(draftStore.slots.get('6|beta')).toBe('survives a slot switch')
    second.unmount()

    render(<Harness actions={actions} composer={{ ...composer, draftStore }} />)
    const again = await openComposer('beta')
    expect(again).toHaveValue('survives a slot switch')
    // Submitting clears this passage's slot.
    fireEvent.keyDown(again, { key: 'Enter' })
    expect(draftStore.clear).toHaveBeenLastCalledWith('beta', 6)
    expect(composer.onSubmit).toHaveBeenCalledWith('survives a slot switch', 'beta')
  })

  it('a confirmed Escape discard clears the persisted draft; emptying the input clears it too', async () => {
    const draftStore = makeStore()
    const confirmDiscard = vi.fn<() => Promise<boolean>>().mockResolvedValue(true)
    render(<Harness actions={actions} composer={{ ...composer, draftStore, confirmDiscard }} />)
    const input = await openComposer()
    fireEvent.change(input, { target: { value: 'x' } })
    fireEvent.change(input, { target: { value: '' } })
    expect(draftStore.clear).toHaveBeenCalledTimes(1)
    fireEvent.change(input, { target: { value: 'gone on purpose' } })
    fireEvent.keyDown(input, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('selection-composer')).toBeNull())
    expect(draftStore.slots.size).toBe(0)
  })

  it('a saved draft is NOT restored onto a twin passage (same text, different position)', async () => {
    const draftStore = makeStore()
    draftStore.slots.set('99|beta', 'about the first beta')
    render(<Harness actions={actions} composer={{ ...composer, draftStore }} />)
    const input = await openComposer('beta') // this "beta" starts at 6, not 99
    expect(input).toHaveValue('')
    expect(draftStore.slots.get('99|beta')).toBe('about the first beta')
  })

  it('a suspended (hidden-tab) toolbar ignores a global Escape', async () => {
    const confirmDiscard = vi.fn<() => Promise<boolean>>().mockResolvedValue(true)
    const { rerender } = render(<Harness actions={actions} composer={{ ...composer, confirmDiscard }} />)
    const input = await openComposer()
    fireEvent.change(input, { target: { value: 'kept while hidden' } })
    rerender(<Harness actions={actions} composer={{ ...composer, confirmDiscard }} suspended />)
    await waitFor(() => expect(screen.queryByTestId('selection-composer')).toBeNull())
    fireEvent.keyUp(document.body, { key: 'Escape' })
    await act(async () => { await new Promise(r => setTimeout(r, 30)) })
    expect(confirmDiscard).not.toHaveBeenCalled()
    expect(composer.onClose).not.toHaveBeenCalled()
    rerender(<Harness actions={actions} composer={{ ...composer, confirmDiscard }} suspended={false} />)
    expect(await screen.findByLabelText('Comment on the selected text')).toHaveValue('kept while hidden')
  })

  it('Escape from OUTSIDE the box (focus left after a click-away) still asks before discarding a draft', async () => {
    const confirmDiscard = vi.fn<() => Promise<boolean>>().mockResolvedValue(false)
    render(<Harness actions={actions} composer={{ ...composer, confirmDiscard }} />)
    const input = await openComposer()
    fireEvent.change(input, { target: { value: 'kept' } })
    // Click-away with a draft keeps the box but moves focus out of it.
    fireEvent.mouseDown(document.body)
    ;(document.activeElement as HTMLElement | null)?.blur()
    fireEvent.keyUp(document.body, { key: 'Escape' })
    await waitFor(() => expect(confirmDiscard).toHaveBeenCalledTimes(1))
    await act(async () => { await new Promise(r => setTimeout(r, 20)) })
    expect(screen.getByTestId('selection-composer')).toBeInTheDocument()
    expect(input).toHaveValue('kept')
  })

  it('a re-select that keeps the box open drops a lingering "Copy failed" notice', async () => {
    copyToClipboardMock.mockResolvedValue(false)
    render(<Harness actions={actions} composer={composer} />)
    const input = await openComposer('beta')
    fireEvent.keyDown(input, { key: 'c', metaKey: true })
    expect(await screen.findByText(/^Copy failed/)).toBeInTheDocument()
    // No draft, so a new selection re-anchors (box stays open) — the notice
    // belonged to the previous attempt.
    selectInContainer(screen.getByTestId('container'), 'gamma')
    fireEvent.mouseUp(document, { clientX: 120, clientY: 50 })
    await waitFor(() => expect(composer.onOpen).toHaveBeenCalledTimes(2))
    expect(screen.queryByText(/^Copy failed/)).toBeNull()
  })

  it('names the way in for touch: "Tap", not "Press Enter"', async () => {
    touchEnv.touch = true
    try {
      render(<Harness actions={actions} composer={composer} />)
      const input = await openComposer()
      expect(input).toHaveAttribute('placeholder', 'Tap to write a comment…')
    } finally {
      touchEnv.touch = false
    }
  })

  it('on touch, the selection collapsing under the host\u2019s highlight does not close the box', async () => {
    touchEnv.touch = true
    try {
      render(<Harness actions={actions} composer={composer} />)
      await openComposer()
      // The host wraps the selection in <mark>s, which collapses the document
      // selection and fires selectionchange with nothing selected.
      window.getSelection()?.removeAllRanges()
      fireEvent(document, new Event('selectionchange'))
      await act(async () => { await new Promise(r => setTimeout(r, 450)) })
      expect(screen.getByTestId('selection-composer')).toBeInTheDocument()
      expect(composer.onClose).not.toHaveBeenCalled()
    } finally {
      touchEnv.touch = false
    }
  })

  it('follows the selection when the container scrolls instead of dismissing', async () => {
    render(<Harness actions={actions} composer={composer} />)
    const input = await openComposer()
    fireEvent.change(input, { target: { value: 'keep me' } })
    const container = screen.getByTestId('container')
    fireEvent.scroll(container)
    await act(async () => { await new Promise(r => setTimeout(r, 40)) })
    expect(screen.getByTestId('selection-composer')).toBeInTheDocument()
    expect(composer.onClose).not.toHaveBeenCalled()
    expect(input).toHaveValue('keep me')
  })
})

describe('SelectionToolbar — plain row without a composer (chat)', () => {
  it('reports a refused copy instead of flashing a checkmark', async () => {
    const refusing: SelectionAction[] = [{ id: 'copy', icon: <span data-testid="copy-icon" />, label: 'Copy', onClick: () => Promise.resolve(false) }]
    render(<Harness actions={refusing} />)
    selectInContainer(screen.getByTestId('container'), 'beta')
    fireEvent.mouseUp(document, { clientX: 100, clientY: 50 })
    fireEvent.click(await screen.findByRole('button', { name: 'Copy' }))
    expect(await screen.findByText(/^Copy failed/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Copy' }).querySelector('[data-testid="copy-icon"]')).not.toBeNull()
  })

  it('renders the actions as labelled text buttons and no input', async () => {
    render(<Harness actions={actions} />)
    selectInContainer(screen.getByTestId('container'), 'beta')
    fireEvent.mouseUp(document, { clientX: 100, clientY: 50 })
    const copy = await screen.findByRole('button', { name: 'Copy' })
    expect(copy).toHaveTextContent('Copy')
    expect(screen.queryByLabelText('Comment on the selected text')).toBeNull()
    expect(screen.queryByTestId('selection-composer')).toBeNull()
  })
})

describe('rangeFromTextOffset', () => {
  it('rebuilds a range across split text nodes from character offsets', () => {
    const root = document.createElement('div')
    root.append('alpha ', document.createElement('mark'), ' gamma')
    root.querySelector('mark')!.append('beta')
    // "alpha beta gamma" → "beta" is [6, 10)
    const range = rangeFromTextOffset(root, 6, 4)
    expect(range?.toString()).toBe('beta')
    // Spanning the mark boundary on both sides.
    expect(rangeFromTextOffset(root, 4, 8)?.toString()).toBe('a beta g')
  })

  it('returns null for an empty or out-of-range request', () => {
    const root = document.createElement('div')
    root.textContent = 'short'
    expect(rangeFromTextOffset(root, 0, 0)).toBeNull()
    expect(rangeFromTextOffset(root, 3, 10)).toBeNull()
  })
})
