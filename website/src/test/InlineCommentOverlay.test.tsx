import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render } from '@testing-library/react'
import { useRef } from 'react'
import { InlineCommentOverlay } from '../components/InlineCommentOverlay'
import type { ArtifactComment } from '../types'

class FakeRO {
  observe() {}
  unobserve() {}
  disconnect() {}
}

let origRaf: typeof globalThis.requestAnimationFrame
let origCaf: typeof globalThis.cancelAnimationFrame

beforeEach(() => {
  globalThis.ResizeObserver = FakeRO
  origRaf = globalThis.requestAnimationFrame
  origCaf = globalThis.cancelAnimationFrame
  globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { cb(0); return 1 }) as typeof globalThis.requestAnimationFrame
  globalThis.cancelAnimationFrame = (() => {}) as typeof globalThis.cancelAnimationFrame
})

afterEach(() => {
  globalThis.requestAnimationFrame = origRaf
  globalThis.cancelAnimationFrame = origCaf
})

function mk(over: Partial<ArtifactComment> = {}): ArtifactComment {
  return {
    id: 'c1', origin: 'local', scope: 'private', author: 'alice', is_agent: false,
    body: 'b', thread_id: 'c1', status: 'open', sync_state: 'local_only',
    created_at: '', updated_at: '', ...over,
  }
}

function Harness({ comments }: { comments: ArtifactComment[] }) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const textRef = useRef<HTMLDivElement>(null)
  return (
    <div ref={scrollRef} style={{ position: 'relative' }}>
      <div ref={textRef}>The quick brown fox jumps over the lazy dog</div>
      <InlineCommentOverlay
        scrollRef={scrollRef}
        textRef={textRef}
        comments={comments}
        activeId={null}
        onActivate={vi.fn()}
      />
    </div>
  )
}

describe('InlineCommentOverlay', () => {
  it('mounts the overlay layer for an anchored comment without crashing', () => {
    const c = mk({ id: 'a1', anchor: { quote: 'quick brown' } })
    const { container } = render(<Harness comments={[c]} />)
    expect(container.querySelector('.mc-cmt-overlay')).not.toBeNull()
  })

  it('paints no anchor for a resolved comment', () => {
    const c = mk({ id: 'a1', anchor: { quote: 'quick brown' }, status: 'resolved' })
    const { container } = render(<Harness comments={[c]} />)
    expect(container.querySelector('.mc-cmt-rect')).toBeNull()
    expect(container.querySelector('.mc-cmt-bubble')).toBeNull()
  })

  it('draws no box on the Copy buttons under a table when a comment runs past it', () => {
    // A quote is Range.toString(), so a selection from the table into the next
    // paragraph holds the button row's labels. happy-dom lays nothing out, so
    // each range reports one rect on a row of its own: the cells at y 0, the
    // paragraph at y 40, and anything holding the buttons' text at y 100.
    vi.spyOn(Range.prototype, 'getClientRects').mockImplementation(function (this: Range) {
      const t = this.toString()
      const top = t.includes('Copy') ? 100 : t.includes('After') ? 40 : 0
      return [{ left: 0, top, width: 10, height: 10 }] as unknown as DOMRectList
    })
    function TableHarness({ comments }: { comments: ArtifactComment[] }) {
      const scrollRef = useRef<HTMLDivElement>(null)
      const textRef = useRef<HTMLDivElement>(null)
      return (
        <div ref={scrollRef} style={{ position: 'relative' }}>
          <div ref={textRef}>
            <table><tbody><tr><td>per change</td><td>Ryan</td></tr></tbody></table>
            <div className="select-none"><button>Copy Markdown</button><button>Copy CSV</button></div>
            <p>After-table paragraph.</p>
          </div>
          <InlineCommentOverlay scrollRef={scrollRef} textRef={textRef} comments={comments} activeId={null} onActivate={vi.fn()} />
        </div>
      )
    }
    const c = mk({ id: 'a1', anchor: { quote: 'per changeRyanCopy MarkdownCopy CSVAfter-table', start_offset: 0 } })
    // The scroll container is the overlay's parent, so its ref is attached only
    // after the overlay's first layout effect; a second render reaches it.
    const { container, rerender } = render(<TableHarness comments={[c]} />)
    rerender(<TableHarness comments={[{ ...c }]} />)
    const tops = [...container.querySelectorAll<HTMLElement>('.mc-cmt-rect')].map(r => r.style.top)
    expect(tops).toEqual(['0px', '40px'])
    vi.restoreAllMocks()
  })

  it('draws no box over unselectable text between two parts of a comment on the same line', () => {
    // Every range reports one rect on line y 0, placed by where its text sits:
    // "alpha " at x 0, the unselectable "skip" at x 60, " beta" at x 100.
    vi.spyOn(Range.prototype, 'getClientRects').mockImplementation(function (this: Range) {
      const t = this.toString()
      const x = t.startsWith('alpha') ? 0 : t.startsWith('skip') ? 60 : 100
      const w = t.includes('beta') && !t.startsWith('alpha') ? 40 : t.includes('beta') ? 140 : 50
      return [{ left: x, top: 0, width: w, height: 10 }] as unknown as DOMRectList
    })
    function InlineHarness({ comments }: { comments: ArtifactComment[] }) {
      const scrollRef = useRef<HTMLDivElement>(null)
      const textRef = useRef<HTMLDivElement>(null)
      return (
        <div ref={scrollRef} style={{ position: 'relative' }}>
          <div ref={textRef}><p>alpha <span className="select-none">skip</span> beta</p></div>
          <InlineCommentOverlay scrollRef={scrollRef} textRef={textRef} comments={comments} activeId={null} onActivate={vi.fn()} />
        </div>
      )
    }
    const c = mk({ id: 'a1', anchor: { quote: 'alpha skip beta', start_offset: 0 } })
    const { container, rerender } = render(<InlineHarness comments={[c]} />)
    rerender(<InlineHarness comments={[{ ...c }]} />)
    const boxes = [...container.querySelectorAll<HTMLElement>('.mc-cmt-rect')].map(r => [r.style.left, r.style.width])
    expect(boxes).toEqual([['0px', '50px'], ['100px', '40px']])
    vi.restoreAllMocks()
  })
})
