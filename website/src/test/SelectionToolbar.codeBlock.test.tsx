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
import { codeBlock, codeBlockUnit, diffFence, dragInCode, dragInPlainCode, multiFileDiffFence } from './pierreDom'

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

function makeDraftStore() {
  const slots = new Map<string, string>()
  const key = (anchor: string, start: number) => `${start}|${anchor}`
  return {
    slots,
    draftStore: {
      read: vi.fn((anchor: string, start: number) => slots.get(key(anchor, start)) ?? null),
      write: vi.fn((text: string, anchor: string, start: number) => { slots.set(key(anchor, start), text) }),
      clear: vi.fn((anchor: string, start: number) => { slots.delete(key(anchor, start)) }),
      hold: vi.fn(),
      release: vi.fn(),
      isEmpty: vi.fn((anchor: string, start: number) => !slots.has(key(anchor, start))),
    },
  }
}

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

  it('restores one draft across plain-to-shown and shown-to-plain swaps', async () => {
    const { draftStore } = makeDraftStore()
    let view = render(<CodeHarness actions={actions} composer={{ ...composer, draftStore }} />)
    const plain = codeBlockUnit([LINE], 'plain')
    screen.getByTestId('code-slot').append(plain.wrapper)
    dragInPlainCode(plain.wrapper)
    const plainBox = await screen.findByLabelText('Comment on the selected text')
    fireEvent.change(plainBox, { target: { value: 'plain passage draft' } })
    const plainStart = draftStore.write.mock.lastCall![2]
    expect(plainStart).toBeLessThan(-1)
    view.unmount()
    vi.restoreAllMocks()

    view = render(<CodeHarness actions={actions} composer={{ ...composer, draftStore }} />)
    const shown = codeBlockUnit([LINE])
    screen.getByTestId('code-slot').append(shown.wrapper)
    dragInCode(shown.host)
    const shownBox = await screen.findByLabelText('Comment on the selected text')
    expect(draftStore.read.mock.lastCall![1]).toBe(plainStart)
    expect(shownBox).toHaveValue('plain passage draft')
    fireEvent.change(shownBox, { target: { value: 'shown passage draft' } })
    view.unmount()
    vi.restoreAllMocks()

    render(<CodeHarness actions={actions} composer={{ ...composer, draftStore }} />)
    const plainAgain = codeBlockUnit([LINE], 'plain')
    screen.getByTestId('code-slot').append(plainAgain.wrapper)
    dragInPlainCode(plainAgain.wrapper)
    const plainAgainBox = await screen.findByLabelText('Comment on the selected text')
    expect(draftStore.read.mock.lastCall![1]).toBe(plainStart)
    expect(plainAgainBox).toHaveValue('shown passage draft')
  })

  it('keeps identical passages in two plain fences in separate draft slots', async () => {
    const { draftStore } = makeDraftStore()
    const firstView = render(<CodeHarness actions={actions} composer={{ ...composer, draftStore }} />)
    const first = codeBlockUnit([LINE], 'plain')
    const second = codeBlockUnit([LINE], 'plain')
    screen.getByTestId('code-slot').append(first.wrapper, second.wrapper)
    dragInPlainCode(first.wrapper)
    const firstBox = await screen.findByLabelText('Comment on the selected text')
    fireEvent.change(firstBox, { target: { value: 'first plain draft' } })
    const firstStart = draftStore.write.mock.lastCall![2]
    expect(firstStart).toBeLessThan(-1)
    firstView.unmount()
    vi.restoreAllMocks()

    render(<CodeHarness actions={actions} composer={{ ...composer, draftStore }} />)
    const firstAgain = codeBlockUnit([LINE], 'plain')
    const secondAgain = codeBlockUnit([LINE], 'plain')
    screen.getByTestId('code-slot').append(firstAgain.wrapper, secondAgain.wrapper)
    dragInPlainCode(secondAgain.wrapper)
    const secondBox = await screen.findByLabelText('Comment on the selected text')
    const secondStart = draftStore.read.mock.lastCall![1]
    expect(secondStart).toBeLessThan(-1)
    expect(secondStart).not.toBe(firstStart)
    expect(secondBox).toHaveValue('')
    expect(draftStore.read(SELECTED, firstStart)).toBe('first plain draft')
  })

  it('files a plain drag from leading whitespace under the shown-copy key', async () => {
    const { draftStore } = makeDraftStore()
    const source = [['  mycli', ' rm', ' NAME']]
    const plainView = render(<CodeHarness actions={actions} composer={{ ...composer, draftStore }} />)
    const plain = codeBlockUnit(source, 'plain')
    screen.getByTestId('code-slot').append(plain.wrapper)
    dragInPlainCode(plain.wrapper, { end: 15 })
    await screen.findByLabelText('Comment on the selected text')
    const plainStart = draftStore.read.mock.lastCall![1]
    plainView.unmount()
    vi.restoreAllMocks()

    render(<CodeHarness actions={actions} composer={{ ...composer, draftStore }} />)
    const shown = codeBlockUnit(source)
    screen.getByTestId('code-slot').append(shown.wrapper)
    dragInCode(shown.host)
    await screen.findByLabelText('Comment on the selected text')
    expect(draftStore.read.mock.lastCall![1]).toBe(plainStart)
  })

  it('files line three of a plain fence under the shown-copy key', async () => {
    const { draftStore } = makeDraftStore()
    const source = [['first'], ['second'], LINE]
    const plainView = render(<CodeHarness actions={actions} composer={{ ...composer, draftStore }} />)
    const plain = codeBlockUnit(source, 'plain')
    screen.getByTestId('code-slot').append(plain.wrapper)
    dragInPlainCode(plain.wrapper, { lineIndex: 2 })
    await screen.findByLabelText('Comment on the selected text')
    const plainStart = draftStore.read.mock.lastCall![1]
    plainView.unmount()
    vi.restoreAllMocks()

    render(<CodeHarness actions={actions} composer={{ ...composer, draftStore }} />)
    const shown = codeBlockUnit(source)
    screen.getByTestId('code-slot').append(shown.wrapper)
    dragInCode(shown.host, { lineIndex: 2 })
    await screen.findByLabelText('Comment on the selected text')
    expect(draftStore.read.mock.lastCall![1]).toBe(plainStart)
  })

  it('closes a plain-code composer on Escape without rebuilding another range', async () => {
    render(<CodeHarness actions={actions} composer={composer} />)
    const plain = codeBlockUnit([LINE], 'plain')
    screen.getByTestId('code-slot').append(plain.wrapper)
    const { selection } = dragInPlainCode(plain.wrapper)
    fireEvent.keyDown(await screen.findByLabelText('Comment on the selected text'), { key: 'Escape' })
    expect(composer.onClose).toHaveBeenCalled()
    expect(selection.addRange).not.toHaveBeenCalled()
  })

  it('keeps identical passages in separate code blocks in separate draft slots', async () => {
    const slots = new Map<string, string>()
    const key = (anchor: string, start: number) => `${start}|${anchor}`
    const draftStore = {
      read: vi.fn((anchor: string, start: number) => slots.get(key(anchor, start)) ?? null),
      write: vi.fn((text: string, anchor: string, start: number) => { slots.set(key(anchor, start), text) }),
      clear: vi.fn((anchor: string, start: number) => { slots.delete(key(anchor, start)) }),
      hold: vi.fn(),
      release: vi.fn(),
      isEmpty: vi.fn((anchor: string, start: number) => !slots.has(key(anchor, start))),
    }

    const firstView = render(<CodeHarness actions={actions} composer={{ ...composer, draftStore }} />)
    const first = addCode()
    addCode()
    dragInCode(first)
    const firstBox = await screen.findByLabelText('Comment on the selected text')
    fireEvent.change(firstBox, { target: { value: 'first passage draft' } })
    const firstStart = draftStore.write.mock.lastCall![2]
    expect(firstStart).toBeLessThan(-1)
    expect(draftStore.read(SELECTED, firstStart)).toBe('first passage draft')
    firstView.unmount()
    vi.restoreAllMocks()

    render(<CodeHarness actions={actions} composer={{ ...composer, draftStore }} />)
    const firstAgain = addCode()
    const second = addCode()
    dragInCode(second)
    const secondBox = await screen.findByLabelText('Comment on the selected text')
    expect(secondBox).toHaveValue('')
    fireEvent.change(secondBox, { target: { value: 'second passage draft' } })
    const secondStart = draftStore.write.mock.lastCall![2]
    expect(secondStart).toBeLessThan(-1)
    expect(secondStart).not.toBe(firstStart)
    expect(draftStore.read(SELECTED, secondStart)).toBe('second passage draft')
    fireEvent.change(secondBox, { target: { value: '' } })
    expect(draftStore.read(SELECTED, secondStart)).toBeNull()
    expect(draftStore.read(SELECTED, firstStart)).toBe('first passage draft')

    fireEvent.keyDown(secondBox, { key: 'Escape' })
    vi.restoreAllMocks()
    dragInCode(firstAgain)
    await waitFor(() => expect(screen.getByLabelText('Comment on the selected text')).toHaveValue('first passage draft'))
  })

  it('keeps identical passages in separate files of one diff fence in separate draft slots', async () => {
    const { draftStore } = makeDraftStore()
    const firstView = render(<CodeHarness actions={actions} composer={{ ...composer, draftStore }} />)
    const firstDiff = multiFileDiffFence([[LINE], [LINE]], [true, true])
    screen.getByTestId('code-slot').append(firstDiff.wrapper)

    dragInCode(firstDiff.hosts[0])
    const firstBox = await screen.findByLabelText('Comment on the selected text')
    fireEvent.change(firstBox, { target: { value: 'file A draft' } })
    const firstStart = draftStore.write.mock.lastCall![2]
    expect(draftStore.read(SELECTED, firstStart)).toBe('file A draft')
    firstView.unmount()
    vi.restoreAllMocks()

    const secondView = render(<CodeHarness actions={actions} composer={{ ...composer, draftStore }} />)
    const secondDiff = multiFileDiffFence([[LINE], [LINE]], [true, true])
    screen.getByTestId('code-slot').append(secondDiff.wrapper)
    dragInCode(secondDiff.hosts[1])
    const secondBox = await screen.findByLabelText('Comment on the selected text')
    expect(secondBox).toHaveValue('')
    fireEvent.change(secondBox, { target: { value: 'file B draft' } })
    const secondStart = draftStore.write.mock.lastCall![2]
    expect(secondStart).not.toBe(firstStart)
    expect(draftStore.read(SELECTED, secondStart)).toBe('file B draft')
    expect(draftStore.read(SELECTED, firstStart)).toBe('file A draft')
    secondView.unmount()
    vi.restoreAllMocks()

    render(<CodeHarness actions={actions} composer={{ ...composer, draftStore }} />)
    const restoredDiff = multiFileDiffFence([[LINE], [LINE]], [true, true])
    screen.getByTestId('code-slot').append(restoredDiff.wrapper)
    dragInCode(restoredDiff.hosts[0])
    await waitFor(() => expect(screen.getByLabelText('Comment on the selected text')).toHaveValue('file A draft'))
  })

  it('keeps a later draft isolated when an earlier plain block becomes shown', async () => {
    const slots = new Map<string, string>()
    const key = (anchor: string, start: number) => `${start}|${anchor}`
    const draftStore = {
      read: vi.fn((anchor: string, start: number) => slots.get(key(anchor, start)) ?? null),
      write: vi.fn((text: string, anchor: string, start: number) => { slots.set(key(anchor, start), text) }),
      clear: vi.fn((anchor: string, start: number) => { slots.delete(key(anchor, start)) }),
      hold: vi.fn(),
      release: vi.fn(),
      isEmpty: vi.fn((anchor: string, start: number) => !slots.has(key(anchor, start))),
    }

    const laterView = render(<CodeHarness actions={actions} composer={{ ...composer, draftStore }} />)
    const first = codeBlockUnit([LINE], 'plain')
    const second = codeBlockUnit([LINE])
    screen.getByTestId('code-slot').append(first.wrapper, second.wrapper)
    dragInCode(second.host)
    const laterBox = await screen.findByLabelText('Comment on the selected text')
    fireEvent.change(laterBox, { target: { value: 'later passage draft' } })
    const laterStart = draftStore.write.mock.lastCall![2]
    expect(draftStore.read(SELECTED, laterStart)).toBe('later passage draft')
    laterView.unmount()
    vi.restoreAllMocks()

    render(<CodeHarness actions={actions} composer={{ ...composer, draftStore }} />)
    const firstShown = codeBlockUnit([LINE])
    const secondShown = codeBlockUnit([LINE])
    screen.getByTestId('code-slot').append(firstShown.wrapper, secondShown.wrapper)
    dragInCode(firstShown.host)
    const earlierBox = await screen.findByLabelText('Comment on the selected text')
    expect(earlierBox).toHaveValue('')
    const earlierStart = draftStore.read.mock.lastCall![1]
    expect(earlierStart).not.toBe(laterStart)
    expect(draftStore.read(SELECTED, laterStart)).toBe('later passage draft')
  })

  it('keeps a later draft isolated when a diff fence ahead of it loses its Pierre host', async () => {
    const slots = new Map<string, string>()
    const key = (anchor: string, start: number) => `${start}|${anchor}`
    const draftStore = {
      read: vi.fn((anchor: string, start: number) => slots.get(key(anchor, start)) ?? null),
      write: vi.fn((text: string, anchor: string, start: number) => { slots.set(key(anchor, start), text) }),
      clear: vi.fn((anchor: string, start: number) => { slots.delete(key(anchor, start)) }),
      hold: vi.fn(),
      release: vi.fn(),
      isEmpty: vi.fn((anchor: string, start: number) => !slots.has(key(anchor, start))),
    }

    const withHostView = render(<CodeHarness actions={actions} composer={{ ...composer, draftStore }} />)
    const diff = diffFence([['-old'], ['+new']])
    const second = codeBlockUnit([LINE])
    const third = codeBlockUnit([LINE])
    screen.getByTestId('code-slot').append(diff.wrapper, second.wrapper, third.wrapper)
    dragInCode(second.host)
    const secondBox = await screen.findByLabelText('Comment on the selected text')
    fireEvent.change(secondBox, { target: { value: 'second fence draft' } })
    const secondStart = draftStore.write.mock.lastCall![2]
    withHostView.unmount()
    vi.restoreAllMocks()

    render(<CodeHarness actions={actions} composer={{ ...composer, draftStore }} />)
    const plainDiff = diffFence([['-old'], ['+new']], false)
    const secondAgain = codeBlockUnit([LINE])
    const thirdAgain = codeBlockUnit([LINE])
    screen.getByTestId('code-slot').append(plainDiff.wrapper, secondAgain.wrapper, thirdAgain.wrapper)
    dragInCode(thirdAgain.host)
    const thirdBox = await screen.findByLabelText('Comment on the selected text')
    expect(thirdBox).toHaveValue('')
    expect(draftStore.read.mock.lastCall![1]).not.toBe(secondStart)
    expect(draftStore.read(SELECTED, secondStart)).toBe('second fence draft')
  })

  it('files a drag that starts outside every source line under the shared fallback slot', async () => {
    const write = vi.fn()
    render(<CodeHarness actions={actions} composer={{ ...composer, draftStore: { read: () => null, write, clear: vi.fn(), hold: vi.fn(), release: vi.fn(), isEmpty: () => true } }} />)
    const host = addCode()
    const selection = dragInCode(host)
    const outside = document.createRange()
    outside.setStart(host.shadowRoot!.querySelector('[data-content]')!, 0)
    outside.setEnd(host.shadowRoot!.querySelector('[data-line]:last-child span:last-child')!.firstChild!, 3)
    selection.getComposedRanges.mockReturnValue([outside])
    fireEvent.mouseUp(document, { clientX: 100, clientY: 50 })
    fireEvent.change(await screen.findByLabelText('Comment on the selected text'), { target: { value: 'a draft' } })
    expect(write).toHaveBeenLastCalledWith('a draft', SELECTED, -1)
  })

  it('keeps an out-of-range passage in the fallback slot and the next row in its own slot', async () => {
    const { draftStore } = makeDraftStore()
    render(<CodeHarness actions={actions} composer={{ ...composer, draftStore }} />)
    const unit = codeBlockUnit([['x'.repeat(100_000), ' ', 'target'], ['target', ' ', 'x']])
    screen.getByTestId('code-slot').append(unit.wrapper)

    const selection = dragInCode(unit.host, { text: 'target' })
    const firstLineTokens = unit.host.shadowRoot!.querySelectorAll('[data-line]')[0].querySelectorAll('span')
    const outOfRange = document.createRange()
    outOfRange.setStart(firstLineTokens[0].firstChild!, 100_000)
    outOfRange.setEnd(firstLineTokens[2].firstChild!, 'target'.length)
    selection.getComposedRanges.mockReturnValue([outOfRange])
    fireEvent.mouseUp(document, { clientX: 100, clientY: 50 })
    const fallbackBox = await screen.findByLabelText('Comment on the selected text')
    fireEvent.change(fallbackBox, { target: { value: 'fallback draft' } })
    expect(draftStore.write).toHaveBeenLastCalledWith('fallback draft', 'target', -1)

    fireEvent.keyDown(fallbackBox, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('selection-composer')).toBeNull())
    vi.restoreAllMocks()
    const nextSelection = dragInCode(unit.host, { lineIndex: 1, text: 'target' })
    const nextText = unit.host.shadowRoot!.querySelectorAll('[data-line]')[1].querySelector('span')!.firstChild!
    const nextRange = document.createRange()
    nextRange.setStart(nextText, 0)
    nextRange.setEnd(nextText, 'target'.length)
    nextSelection.getComposedRanges.mockReturnValue([nextRange])
    fireEvent.mouseUp(document, { clientX: 100, clientY: 50 })
    const nextBox = await screen.findByLabelText('Comment on the selected text')
    const nextStart = draftStore.read.mock.lastCall![1]
    expect(nextStart).toBeLessThan(-1)
    expect(nextStart).not.toBe(-1)
    expect(nextBox).toHaveValue('')
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

    it('moves the box with a plain fence by its live range', async () => {
      let selectedRange: Range | null = null
      Object.defineProperty(Range.prototype, 'getBoundingClientRect', {
        configurable: true,
        value(this: Range) {
          return this === selectedRange ? new DOMRect(300, codeTop, 80, 18) : new DOMRect(10, 10, 100, 20)
        },
      })
      render(<CodeHarness actions={actions} composer={composer} />)
      const plain = codeBlockUnit([LINE], 'plain')
      screen.getByTestId('code-slot').append(plain.wrapper)
      selectedRange = dragInPlainCode(plain.wrapper).range
      const box = await boxOf()
      await waitFor(() => expect(box.style.top).toBe('292px'))
      codeTop = 100
      await scroll()
      expect(box.style.top).toBe('92px')
    })

    it('moves the box with prose the host replaced, from its text offsets', async () => {
      let replacement: Text | null = null
      Object.defineProperty(Range.prototype, 'getBoundingClientRect', {
        configurable: true,
        value(this: Range) {
          return this.startContainer === replacement ? new DOMRect(300, codeTop, 80, 18) : new DOMRect(10, 10, 100, 20)
        },
      })
      const repaint = () => {
        const paragraph = screen.getByText(PROSE)
        replacement = document.createTextNode(PROSE)
        paragraph.replaceChildren(replacement)
      }
      render(<CodeHarness actions={actions} composer={{ ...composer, onOpen: repaint }} />)
      const prose = screen.getByText(PROSE).firstChild as Text
      const range = document.createRange()
      range.setStart(prose, 5)
      range.setEnd(prose, 10)
      vi.spyOn(window, 'getSelection').mockReturnValue({ isCollapsed: false, rangeCount: 1, toString: () => 'prose', getRangeAt: () => range, addRange: vi.fn(), removeAllRanges: vi.fn() } as unknown as Selection)
      fireEvent.mouseUp(document, { clientX: 100, clientY: 50 })
      const box = await boxOf()
      await waitFor(() => expect(box.style.top).toBe('38px'))
      await scroll()
      expect(box.style.top).toBe('292px')
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

  it('keeps a plain-fence selection on the ordinary slot when the host did not opt in', () => {
    const { draftStore } = makeDraftStore()
    render(<CodeHarness actions={actions} composer={{ ...composer, draftStore }} optIn={false} />)
    const plain = codeBlockUnit([LINE], 'plain')
    screen.getByTestId('code-slot').append(plain.wrapper)
    const { selection } = dragInPlainCode(plain.wrapper)
    lookAtSelection()

    const input = screen.getByLabelText('Comment on the selected text')
    fireEvent.change(input, { target: { value: 'ordinary passage draft' } })
    const start = draftStore.write.mock.lastCall![2]
    expect(start).toBeGreaterThanOrEqual(0)

    fireEvent.keyDown(input, { key: 'Escape' })
    expect(composer.onClose).toHaveBeenCalled()
    const restored = selection.addRange.mock.lastCall![0] as Range
    expect(restored.toString()).toBe(SELECTED)
  })

  it('keeps passages in a plain diff body on their ordinary slots', () => {
    const { draftStore } = makeDraftStore()
    render(<CodeHarness actions={actions} composer={{ ...composer, draftStore }} />)
    const diff = diffFence([LINE, LINE], false)
    diff.wrapper.querySelector('pre')!.className = 'pierre-plain'
    screen.getByTestId('code-slot').append(diff.wrapper)

    const { selection } = dragInPlainCode(diff.wrapper)
    lookAtSelection()
    const firstInput = screen.getByLabelText('Comment on the selected text')
    fireEvent.change(firstInput, { target: { value: 'first diff draft' } })
    const firstStart = draftStore.write.mock.lastCall![2]
    expect(firstStart).toBeGreaterThanOrEqual(0)
    fireEvent.keyDown(firstInput, { key: 'Escape' })
    const restored = selection.addRange.mock.lastCall![0] as Range
    expect(restored.toString()).toBe(SELECTED)
    vi.restoreAllMocks()

    dragInPlainCode(diff.wrapper, { lineIndex: 1 })
    lookAtSelection()
    fireEvent.change(screen.getByLabelText('Comment on the selected text'), { target: { value: 'second diff draft' } })
    const secondStart = draftStore.write.mock.lastCall![2]
    expect(secondStart).toBeGreaterThanOrEqual(0)
    expect(secondStart).not.toBe(firstStart)
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
