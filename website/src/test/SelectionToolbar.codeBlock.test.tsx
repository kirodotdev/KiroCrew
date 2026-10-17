/**
 * SelectionToolbar over text selected inside a code block.
 *
 * Pierre draws a code block into an open shadow root. A drag inside it leaves the
 * document's Selection COLLAPSED on the host's parent (both ends are retargeted
 * out of the root) while `toString()` still holds the text; only
 * `getComposedRanges` names where the text really is. happy-dom has no selection
 * across a shadow root, so each scene installs a Selection that answers the way
 * Chrome's does.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { useRef } from 'react'
import SelectionToolbar, { type SelectionAction, type SelectionComposer } from '../components/SelectionToolbar'
import { codeBlock, dragInCode } from './pierreDom'

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

const PROSE = 'Some prose to annotate before the code.'
/** The tokens Pierre gives `mycli rm NAME --force`. */
const LINE = ['mycli', ' rm', ' NAME', ' --force']
const SELECTED = 'mycli rm NAME'

function CodeHarness({ actions, composer, externalSelection, optIn = true }: {
  actions: SelectionAction[]
  composer?: SelectionComposer
  externalSelection?: { text: string; x: number; y: number } | null
  /** False leaves `codeSelection` out, as every host but the file panel does. */
  optIn?: boolean
}) {
  const ref = useRef<HTMLDivElement>(null)
  return (
    <div>
      <div ref={ref} data-testid="container"><p>{PROSE}</p><div data-testid="code-slot" /></div>
      <SelectionToolbar containerRef={ref} actions={actions} composer={composer} externalSelection={externalSelection} {...(optIn ? { codeSelection: true } : {})} />
    </div>
  )
}

/** Puts a code block in the container, outside React's tree as Pierre's is. */
function addCode(where: 'container' | 'elsewhere' = 'container'): HTMLElement {
  const host = codeBlock([LINE])
  if (where === 'container') screen.getByTestId('code-slot').append(host)
  else document.body.append(host)
  return host
}

let onCopy: ReturnType<typeof vi.fn>
let actions: SelectionAction[]
let composer: SelectionComposer & { onOpen: ReturnType<typeof vi.fn>; onSubmit: ReturnType<typeof vi.fn>; onClose: ReturnType<typeof vi.fn> }

beforeEach(() => {
  onCopy = vi.fn()
  actions = [{ id: 'copy', icon: <span />, label: 'Copy', onClick: onCopy }]
  composer = { onOpen: vi.fn(), onSubmit: vi.fn(), onClose: vi.fn() }
  copyToClipboardMock.mockReset()
  copyToClipboardMock.mockResolvedValue(true)
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  document.body.querySelectorAll(':scope > diffs-container').forEach(host => host.remove())
})

describe('SelectionToolbar — text selected inside a code block', () => {
  it('opens the comment box and tells the host the selected text', async () => {
    render(<CodeHarness actions={actions} composer={composer} />)
    dragInCode(addCode())
    expect(await screen.findByLabelText('Comment on the selected text')).toBeInTheDocument()
    expect(composer.onOpen).toHaveBeenCalledWith(SELECTED)
  })

  it('submits a comment against that text', async () => {
    render(<CodeHarness actions={actions} composer={composer} />)
    dragInCode(addCode())
    fireEvent.change(await screen.findByLabelText('Comment on the selected text'), { target: { value: 'why this flag?' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add comment' }))
    expect(composer.onSubmit).toHaveBeenCalledWith('why this flag?', SELECTED)
  })

  it('hangs the box from the code’s own range, not from where the engine retargeted it', async () => {
    const rangeRect = Object.getOwnPropertyDescriptor(Range.prototype, 'getBoundingClientRect')
    Object.defineProperty(Range.prototype, 'getBoundingClientRect', {
      configurable: true,
      value(this: Range) {
        const inCode = this.startContainer.getRootNode() instanceof ShadowRoot
        return inCode ? new DOMRect(300, 200, 80, 18) : new DOMRect(10, 10, 100, 20)
      },
    })
    try {
      render(<CodeHarness actions={actions} composer={composer} />)
      dragInCode(addCode())
      const box = (await screen.findByTestId('selection-composer')).parentElement as HTMLElement
      await waitFor(() => expect(box.style.left).toBe('300px'))
    } finally {
      if (rangeRect) Object.defineProperty(Range.prototype, 'getBoundingClientRect', rangeRect)
      else delete (Range.prototype as unknown as Record<string, unknown>).getBoundingClientRect
    }
  })

  it('asks the engine about the code blocks under its container only', async () => {
    render(<CodeHarness actions={actions} composer={composer} />)
    const inside = addCode()
    addCode('elsewhere')
    const selection = dragInCode(inside)
    await screen.findByLabelText('Comment on the selected text')
    expect(selection.getComposedRanges).toHaveBeenCalledWith({ shadowRoots: [inside.shadowRoot] })
  })

  it('copies the selected code from the empty box with Cmd+C', async () => {
    render(<CodeHarness actions={actions} composer={composer} />)
    dragInCode(addCode())
    fireEvent.keyDown(await screen.findByLabelText('Comment on the selected text'), { key: 'c', metaKey: true })
    await waitFor(() => expect(copyToClipboardMock).toHaveBeenCalledWith(SELECTED))
  })

  it('closes on Escape without restoring a selection over the prose, which has no such offsets', async () => {
    render(<CodeHarness actions={actions} composer={composer} />)
    const selection = dragInCode(addCode())
    fireEvent.keyDown(await screen.findByLabelText('Comment on the selected text'), { key: 'Escape' })
    expect(composer.onClose).toHaveBeenCalled()
    expect(selection.addRange).not.toHaveBeenCalled()
  })

  describe('when the panel scrolls', () => {
    // The code is where `codeTop` says; any other text sits at the top left.
    let codeTop: number
    let rangeRect: PropertyDescriptor | undefined
    beforeEach(() => {
      codeTop = 300
      rangeRect = Object.getOwnPropertyDescriptor(Range.prototype, 'getBoundingClientRect')
      Object.defineProperty(Range.prototype, 'getBoundingClientRect', {
        configurable: true,
        value(this: Range) {
          return this.startContainer.getRootNode() instanceof ShadowRoot ? new DOMRect(300, codeTop, 80, 18) : new DOMRect(10, 10, 100, 20)
        },
      })
    })
    afterEach(() => {
      if (rangeRect) Object.defineProperty(Range.prototype, 'getBoundingClientRect', rangeRect)
      else delete (Range.prototype as unknown as Record<string, unknown>).getBoundingClientRect
    })

    const boxOf = async () => (await screen.findByTestId('selection-composer')).parentElement as HTMLElement
    /** A scroll, then the frame the box follows it in and React's render of that. */
    const scroll = () => act(async () => {
      fireEvent.scroll(screen.getByTestId('container'))
      await new Promise(resolve => requestAnimationFrame(resolve))
    })

    it('moves the box with the code, as it does with prose', async () => {
      render(<CodeHarness actions={actions} composer={composer} />)
      dragInCode(addCode())
      const box = await boxOf()
      await waitFor(() => expect(box.style.top).toBe('292px')) // 8px above the code
      codeTop = 100
      await scroll()
      expect(box.style.top).toBe('92px')
    })

    it('does not carry the code’s place over to a later selection of prose', async () => {
      render(<CodeHarness actions={actions} composer={composer} />)
      dragInCode(addCode())
      fireEvent.keyDown(await screen.findByLabelText('Comment on the selected text'), { key: 'Escape' })
      await waitFor(() => expect(screen.queryByTestId('selection-composer')).toBeNull())
      const prose = screen.getByText(PROSE).firstChild as Text
      const range = document.createRange()
      range.setStart(prose, 5)
      range.setEnd(prose, 10)
      vi.spyOn(window, 'getSelection').mockReturnValue({ isCollapsed: false, rangeCount: 1, toString: () => range.toString(), getRangeAt: () => range, addRange: vi.fn(), removeAllRanges: vi.fn() } as unknown as Selection)
      fireEvent.mouseUp(document, { clientX: 100, clientY: 50 })
      const box = await boxOf()
      await waitFor(() => expect(box.style.top).toBe('38px')) // too near the top edge for room above: 8px below the prose
      codeTop = 100
      await scroll()
      expect(box.style.top).toBe('38px')
    })

    it('does not carry the code’s place over to an external selection', async () => {
      const { rerender } = render(<CodeHarness actions={actions} composer={composer} />)
      dragInCode(addCode())
      const box = await boxOf()
      await waitFor(() => expect(box.style.top).toBe('292px'))
      rerender(<CodeHarness actions={actions} composer={composer} externalSelection={{ text: 'from the frame', x: 20, y: 40 }} />)
      await waitFor(() => expect(composer.onOpen).toHaveBeenLastCalledWith('from the frame'))
      const before = box.style.top
      codeTop = 100
      await scroll()
      expect(box.style.top).toBe(before)
    })
  })

  it('a host without a composer gets the plain action row, whose actions receive the selected text', async () => {
    render(<CodeHarness actions={actions} />)
    dragInCode(addCode())
    fireEvent.click(await screen.findByRole('button', { name: 'Copy' }))
    expect(onCopy).toHaveBeenCalledWith(SELECTED, expect.anything())
    expect(screen.queryByLabelText('Comment on the selected text')).toBeNull()
  })
})

/** The toolbar looks at the selection 50ms after the mouse is released. */
const lookAtSelection = () => act(() => { vi.advanceTimersByTime(60) })

describe('SelectionToolbar — selections that are not annotatable code', () => {
  beforeEach(() => { vi.useFakeTimers() })

  it('ignores a selection inside a code block outside the container', () => {
    render(<CodeHarness actions={actions} composer={composer} />)
    dragInCode(addCode('elsewhere'))
    lookAtSelection()
    expect(screen.queryByLabelText('Comment on the selected text')).toBeNull()
    expect(composer.onOpen).not.toHaveBeenCalled()
  })

  it('ignores a collapsed selection holding no text, without asking the engine anything', () => {
    render(<CodeHarness actions={actions} composer={composer} />)
    const selection = dragInCode(addCode(), { text: '' })
    lookAtSelection()
    expect(screen.queryByLabelText('Comment on the selected text')).toBeNull()
    expect(selection.getComposedRanges).not.toHaveBeenCalled()
  })

  it('ignores a selection inside a code block where the host did not opt in, without asking the engine', () => {
    render(<CodeHarness actions={actions} composer={composer} optIn={false} />)
    const selection = dragInCode(addCode())
    lookAtSelection()
    expect(screen.queryByLabelText('Comment on the selected text')).toBeNull()
    expect(composer.onOpen).not.toHaveBeenCalled()
    expect(selection.getComposedRanges).not.toHaveBeenCalled()
  })

  it('does not ask the engine about an ordinary selection of the prose', () => {
    render(<CodeHarness actions={actions} composer={composer} />)
    addCode()
    const prose = screen.getByText(PROSE).firstChild as Text
    const range = document.createRange()
    range.setStart(prose, 5)
    range.setEnd(prose, 10)
    const getComposedRanges = vi.fn()
    const selection = { isCollapsed: false, rangeCount: 1, toString: () => range.toString(), getRangeAt: () => range, getComposedRanges, addRange: vi.fn(), removeAllRanges: vi.fn() }
    vi.spyOn(window, 'getSelection').mockReturnValue(selection as unknown as Selection)
    fireEvent.mouseUp(document, { clientX: 100, clientY: 50 })
    lookAtSelection()
    expect(composer.onOpen).toHaveBeenCalledWith('prose')
    expect(getComposedRanges).not.toHaveBeenCalled()
  })

  it('behaves as before in an engine that cannot name the range', () => {
    render(<CodeHarness actions={actions} composer={composer} />)
    dragInCode(addCode(), { withComposedRanges: false })
    lookAtSelection()
    expect(screen.queryByLabelText('Comment on the selected text')).toBeNull()
    expect(composer.onOpen).not.toHaveBeenCalled()
  })
})
