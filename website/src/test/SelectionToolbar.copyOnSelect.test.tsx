/**
 * Copy on select in the chat transcript: with `copyOnSelect`, the end of a MOUSE
 * drag runs the row's own Copy action, so the selection lands on the clipboard
 * without a second click, and the Quote / Ask / Copy row still opens.
 *
 * The row here is built by `useSelectionActions`, the same hook the chat
 * transcript uses, so the Copy action under test is the production one (only
 * the clipboard helper underneath it is mocked).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act, cleanup } from '@testing-library/react'
import { useRef } from 'react'
import SelectionToolbar, { useSelectionActions } from '../components/SelectionToolbar'

// Skip animations so hide/show is synchronous.
vi.mock('framer-motion', async () => {
  const { forwardRef } = await import('react')
  return {
    AnimatePresence: ({ children }: { children: React.ReactNode }) => children,
    motion: {
      div: forwardRef(({ children, ...props }: Record<string, unknown> & { children?: React.ReactNode }, ref: React.Ref<HTMLDivElement>) => {
        const { initial: _i, animate: _a, exit: _e, transition: _t, ...rest } = props as Record<string, unknown>
        return <div ref={ref} {...(rest as React.HTMLAttributes<HTMLDivElement>)}>{children}</div>
      }),
    },
  }
})

if (!Range.prototype.getBoundingClientRect) {
  Range.prototype.getBoundingClientRect = function () { return new DOMRect(10, 10, 100, 20) }
}

const copyToClipboardMock = vi.fn<(text: string) => Promise<boolean>>()
vi.mock('../utils/clipboard', () => ({ copyToClipboard: (text: string) => copyToClipboardMock(text) }))
vi.mock('../utils/isTouchDevice', () => ({ isTouchDevice: () => false }))

const TEXT = 'alpha beta   gamma'

function select(container: HTMLElement, start: number, end: number) {
  const textNode = container.firstChild as Text
  const range = document.createRange()
  range.setStart(textNode, start)
  range.setEnd(textNode, end)
  const sel = window.getSelection()!
  sel.removeAllRanges()
  sel.addRange(range)
}

function selectWord(container: HTMLElement, word: string) {
  const start = TEXT.indexOf(word)
  select(container, start, start + word.length)
}

function Harness({ copyOnSelect }: { copyOnSelect?: boolean }) {
  const ref = useRef<HTMLDivElement>(null)
  const actions = useSelectionActions(() => {}, () => {})
  return (
    <div>
      <div ref={ref} data-testid="container">{TEXT}</div>
      <SelectionToolbar containerRef={ref} actions={actions} copyOnSelect={copyOnSelect} />
    </div>
  )
}

/** Release the mouse and let the 50ms "selection settles" check run. */
async function mouseUp(button = 0) {
  await act(async () => {
    document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, button, clientX: 40, clientY: 20 }))
    await vi.advanceTimersByTimeAsync(60)
  })
}

const copyButton = () => screen.getByRole('button', { name: 'Copy' })
const showsCheckmark = () => copyButton().querySelector('svg.text-ok') !== null

describe('SelectionToolbar copy on select', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    copyToClipboardMock.mockReset()
    copyToClipboardMock.mockResolvedValue(true)
  })
  afterEach(() => {
    cleanup()
    window.getSelection()?.removeAllRanges()
    vi.useRealTimers()
  })

  it('copies nothing by default: the row opens and waits for a click', async () => {
    render(<Harness />)
    selectWord(screen.getByTestId('container'), 'beta')
    await mouseUp()

    expect(screen.getByRole('button', { name: 'Quote' })).toBeInTheDocument()
    expect(copyToClipboardMock).not.toHaveBeenCalled()
    expect(showsCheckmark()).toBe(false)
  })

  it('copies the selection as the drag ends and still opens the row', async () => {
    render(<Harness copyOnSelect />)
    selectWord(screen.getByTestId('container'), 'beta')
    await mouseUp()

    expect(copyToClipboardMock).toHaveBeenCalledTimes(1)
    expect(copyToClipboardMock).toHaveBeenCalledWith('beta')
    // The row is open, Quote and Ask still one click away, and Copy shows the
    // same confirmation a click would.
    expect(screen.getByRole('button', { name: 'Quote' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Ask about this' })).toBeInTheDocument()
    expect(showsCheckmark()).toBe(true)
  })

  it('copies the same text a click on Copy would', async () => {
    render(<Harness copyOnSelect />)
    // A drag that starts and ends on whitespace: Copy takes the trimmed passage.
    select(screen.getByTestId('container'), TEXT.indexOf('beta') - 1, TEXT.indexOf('gamma'))
    await mouseUp()
    expect(copyToClipboardMock).toHaveBeenLastCalledWith('beta')

    await act(async () => { copyButton().click() })
    expect(copyToClipboardMock).toHaveBeenCalledTimes(2)
    expect(copyToClipboardMock.mock.calls[1][0]).toBe(copyToClipboardMock.mock.calls[0][0])
  })

  it('does not copy a whitespace-only selection (and opens no row)', async () => {
    render(<Harness copyOnSelect />)
    const gap = TEXT.indexOf('   ')
    select(screen.getByTestId('container'), gap, gap + 3)
    await mouseUp()

    expect(copyToClipboardMock).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: 'Copy' })).toBeNull()
  })

  it('does not copy on a plain click (a collapsed selection)', async () => {
    render(<Harness copyOnSelect />)
    select(screen.getByTestId('container'), 3, 3)
    await mouseUp()

    expect(copyToClipboardMock).not.toHaveBeenCalled()
  })

  it('reports a refused clipboard write and keeps the row open', async () => {
    copyToClipboardMock.mockResolvedValue(false)
    render(<Harness copyOnSelect />)
    selectWord(screen.getByTestId('container'), 'beta')
    await mouseUp()

    expect(copyToClipboardMock).toHaveBeenCalledWith('beta')
    expect(screen.getByText(/^Copy failed/)).toBeInTheDocument()
    expect(showsCheckmark()).toBe(false)
    expect(screen.getByRole('button', { name: 'Quote' })).toBeInTheDocument()
  })

  it('does not copy on a right-click that selects a word (macOS context menu)', async () => {
    render(<Harness copyOnSelect />)
    // A Mac browser selects the word under the pointer before `contextmenu`,
    // then the button-2 release reaches the document.
    selectWord(screen.getByTestId('container'), 'beta')
    await mouseUp(2)

    expect(copyToClipboardMock).not.toHaveBeenCalled()
    expect(showsCheckmark()).toBe(false)
  })

  it('leaves a keyboard (Shift+Arrow) selection alone', async () => {
    render(<Harness copyOnSelect />)
    selectWord(screen.getByTestId('container'), 'beta')
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keyup', { key: 'ArrowRight', shiftKey: true, bubbles: true }))
      await vi.advanceTimersByTimeAsync(60)
    })

    // The row opens as before; only a mouse drag copies.
    expect(screen.getByRole('button', { name: 'Quote' })).toBeInTheDocument()
    expect(copyToClipboardMock).not.toHaveBeenCalled()
  })

  it('does not copy again on a later click that leaves the selection in place', async () => {
    render(<Harness copyOnSelect />)
    selectWord(screen.getByTestId('container'), 'beta')
    await mouseUp()
    expect(copyToClipboardMock).toHaveBeenCalledTimes(1)

    // A click on a control outside the row whose mousedown keeps the document
    // selection (another Copy button, say): the press changes nothing, so the
    // clipboard must keep what that control wrote.
    const other = document.createElement('button')
    other.addEventListener('mousedown', e => e.preventDefault())
    document.body.appendChild(other)
    try {
      await act(async () => {
        other.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }))
      })
      await act(async () => {
        other.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, clientX: 40, clientY: 20 }))
        await vi.advanceTimersByTimeAsync(60)
      })
      expect(copyToClipboardMock).toHaveBeenCalledTimes(1)
    } finally {
      other.remove()
    }
  })

  it('copies a new drag made after an earlier one', async () => {
    render(<Harness copyOnSelect />)
    const container = screen.getByTestId('container')
    selectWord(container, 'beta')
    await mouseUp()

    // The next drag starts with the old selection still in the document.
    await act(async () => {
      container.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }))
    })
    selectWord(container, 'gamma')
    await mouseUp()

    expect(copyToClipboardMock).toHaveBeenCalledTimes(2)
    expect(copyToClipboardMock).toHaveBeenLastCalledWith('gamma')
  })

  it('stops copying as soon as the setting is turned off', async () => {
    const view = render(<Harness copyOnSelect />)
    view.rerender(<Harness copyOnSelect={false} />)
    selectWord(screen.getByTestId('container'), 'beta')
    await mouseUp()

    expect(copyToClipboardMock).not.toHaveBeenCalled()
  })
})
