/**
 * The comment flow end to end over a highlighted code block: the toolbar's
 * composer opens for a selection Chromium reports retargeted to the block's
 * host, and the anchored-comment overlay finds a code anchor once the rows land
 * in the shadow tree (which happens without a reflow, so no resize fires).
 */
import { describe, it, expect, vi, beforeEach, afterEach, onTestFinished } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { useRef } from 'react'
import SelectionToolbar, { type SelectionComposer } from '../components/SelectionToolbar'
import { InlineCommentOverlay } from '../components/InlineCommentOverlay'
import { indexComposedText, rangeForTextSpan } from '../utils/composedText'
import type { ArtifactComment } from '../types'

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
vi.mock('../utils/isTouchDevice', () => ({ isTouchDevice: () => false }))

const LINES = ['<service name="orders">', '  <timeout unit="ms">2500</timeout>', '</service>']

/** Fill `host`'s open shadow root with Pierre-shaped rows. */
function fillRows(host: HTMLElement) {
  const sr = host.shadowRoot ?? host.attachShadow({ mode: 'open' })
  const pre = document.createElement('pre')
  for (const [i, line] of LINES.entries()) {
    const row = document.createElement('div')
    row.setAttribute('data-line', String(i + 1))
    const s = document.createElement('span')
    s.textContent = line
    row.appendChild(s)
    pre.appendChild(row)
  }
  sr.appendChild(pre)
}

const rectProto = Range.prototype as unknown as { getBoundingClientRect?: () => DOMRect; getClientRects?: () => DOMRect[] }
const origBounding = rectProto.getBoundingClientRect
const origRects = rectProto.getClientRects

beforeEach(() => {
  rectProto.getBoundingClientRect = function () { return new DOMRect(10, 10, 100, 20) }
  rectProto.getClientRects = function (this: Range) { return this.collapsed ? [] : [new DOMRect(40, 60, 30, 16)] }
})
afterEach(() => {
  rectProto.getBoundingClientRect = origBounding
  rectProto.getClientRects = origRects
  vi.restoreAllMocks()
})

describe('commenting on a highlighted code block', () => {
  it('opens the composer for a selection made inside the block', async () => {
    const composer: SelectionComposer & { onOpen: ReturnType<typeof vi.fn> } = { onOpen: vi.fn(), onSubmit: vi.fn(), onClose: vi.fn() }
    function Harness() {
      const ref = useRef<HTMLDivElement>(null)
      return (
        <div>
          <div ref={ref} data-testid="body">
            <p>We propose this XML.</p>
            <div data-testid="block" data-anchorable-code="" ref={el => { if (el && !el.firstChild) { const h = document.createElement('diffs-container'); el.appendChild(h); fillRows(h) } }} />
          </div>
          <SelectionToolbar containerRef={ref} actions={[]} composer={composer} />
        </div>
      )
    }
    render(<Harness />)
    const block = screen.getByTestId('block')
    const host = block.firstChild as HTMLElement
    const idx = indexComposedText(host)
    const at = idx.text.indexOf('2500')
    const real = rangeForTextSpan(idx, at, at + 4)!
    // Chromium's report of a drag inside the shadow tree: collapsed at the
    // host's parent, the code still in toString(), the real range only from
    // getComposedRanges.
    const collapsed = document.createRange()
    collapsed.setStart(block, 0)
    const fake = {
      rangeCount: 1, anchorNode: block, focusNode: block, isCollapsed: true,
      getRangeAt: () => collapsed,
      toString: () => '2500',
      getComposedRanges: (o: { shadowRoots: ShadowRoot[] }) => (o.shadowRoots.includes(host.shadowRoot!) ? [real] : [collapsed]),
      removeAllRanges: () => {}, addRange: () => {},
    } as unknown as Selection
    vi.spyOn(window, 'getSelection').mockReturnValue(fake)
    fireEvent.mouseUp(document, { clientX: 100, clientY: 50 })
    expect(await screen.findByLabelText('Comment on the selected text')).toBeInTheDocument()
    expect(composer.onOpen).toHaveBeenCalledWith('2500')
  })

  it('draws the anchor once the block\'s rows land in its shadow tree', async () => {
    class FakeRO { observe() {} unobserve() {} disconnect() {} }
    const origRO = globalThis.ResizeObserver
    globalThis.ResizeObserver = FakeRO as unknown as typeof ResizeObserver
    onTestFinished(() => { globalThis.ResizeObserver = origRO })
    const origRaf = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { cb(0); return 1 }) as typeof globalThis.requestAnimationFrame
    onTestFinished(() => { globalThis.requestAnimationFrame = origRaf })
    const comment: ArtifactComment = {
      id: 'c1', origin: 'local', scope: 'private', author: 'a', is_agent: false, body: 'b', thread_id: 'c1',
      status: 'open', sync_state: 'local_only', created_at: '', updated_at: '',
      anchor: { quote: '2500', prefix: '<timeout unit="ms">', suffix: '</timeout>' },
    }
    let host: HTMLElement | null = null
    function Harness() {
      const scrollRef = useRef<HTMLDivElement>(null)
      const textRef = useRef<HTMLDivElement>(null)
      return (
        <div ref={scrollRef} style={{ position: 'relative' }}>
          <div ref={textRef}>
            <p>We propose this XML.</p>
            <div data-anchorable-code="" ref={el => { if (el && !el.firstChild) { host = document.createElement('diffs-container'); host.attachShadow({ mode: 'open' }); el.appendChild(host) } }} />
          </div>
          <InlineCommentOverlay scrollRef={scrollRef} textRef={textRef} comments={[comment]} activeId={null} onActivate={vi.fn()} />
        </div>
      )
    }
    const { container, rerender } = render(<Harness />)
    // The scroller's ref attaches after the overlay's first layout effect; the
    // pages re-run it when the comment list arrives, as this re-render does.
    rerender(<Harness />)
    expect(container.querySelector('.mc-cmt-rect')).toBeNull()
    await act(async () => { fillRows(host!) })
    await waitFor(() => expect(container.querySelector('.mc-cmt-rect')).not.toBeNull())
    expect(container.querySelector('.mc-cmt-bubble')?.textContent).toBe('1')
  })
})
