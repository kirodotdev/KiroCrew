/**
 * InlineCommentOverlay: the highlight layer drawn over rendered markdown.
 *
 * happy-dom lays nothing out, so Range.getClientRects is stubbed to return the boxes a
 * reader would see. The tests assert the boxes that reach the DOM, which thread a click
 * or key opens, and where the body scrolls to when the sidebar asks for an anchor.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render } from '@testing-library/react'
import { useRef } from 'react'

import { InlineCommentOverlay } from './InlineCommentOverlay'
import type { ArtifactComment } from '../types'

class FakeRO {
  observe() {}
  unobserve() {}
  disconnect() {}
}

let origRaf: typeof globalThis.requestAnimationFrame
let origCaf: typeof globalThis.cancelAnimationFrame

beforeEach(() => {
  globalThis.ResizeObserver = FakeRO as unknown as typeof ResizeObserver
  origRaf = globalThis.requestAnimationFrame
  origCaf = globalThis.cancelAnimationFrame
  globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { cb(0); return 1 }) as typeof globalThis.requestAnimationFrame
  globalThis.cancelAnimationFrame = (() => {}) as typeof globalThis.cancelAnimationFrame
})

afterEach(() => {
  globalThis.requestAnimationFrame = origRaf
  globalThis.cancelAnimationFrame = origCaf
  vi.restoreAllMocks()
})

function mk(over: Partial<ArtifactComment> = {}): ArtifactComment {
  return {
    id: 'c1', origin: 'local', scope: 'private', author: 'alice', is_agent: false,
    body: 'b', thread_id: 'c1', status: 'open', sync_state: 'local_only',
    created_at: '', updated_at: '', ...over,
  }
}

interface HarnessProps {
  comments: ArtifactComment[]
  activeId?: string | null
  scrollNonce?: number
  onActivate?: (id: string) => void
}

function Harness({ comments, activeId = null, scrollNonce, onActivate = vi.fn() }: HarnessProps) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const textRef = useRef<HTMLDivElement>(null)
  return (
    <div ref={scrollRef} style={{ position: 'relative' }}>
      <div ref={textRef}><p>alpha beta gamma</p></div>
      <InlineCommentOverlay
        scrollRef={scrollRef}
        textRef={textRef}
        comments={comments}
        activeId={activeId}
        scrollNonce={scrollNonce}
        onActivate={onActivate}
      />
    </div>
  )
}

/** Mount, then re-render once: the scroll container's ref is attached after the first layout pass. */
function mount(props: HarnessProps) {
  const view = render(<Harness {...props} />)
  // A fresh comment array re-runs the layout pass, which now sees the attached refs.
  view.rerender(<Harness {...props} comments={props.comments.map(c => ({ ...c }))} />)
  return view
}

const anchored = mk({ id: 'a1', anchor: { quote: 'alpha beta', start_offset: 0 } })

describe('InlineCommentOverlay rect merging', () => {
  it('merges same-line fragments of one anchor into a single highlight box', () => {
    // Two fragments on the same line (same rounded y), plus a zero-size fragment that is dropped.
    vi.spyOn(Range.prototype, 'getClientRects').mockImplementation(function (this: Range) {
      return [
        { left: 0, top: 0, width: 50, height: 10 },
        { left: 60, top: 1, width: 40, height: 12 },
        { left: 5, top: 5, width: 0, height: 10 },
      ] as unknown as DOMRectList
    })
    const { container } = mount({ comments: [anchored] })
    const boxes = [...container.querySelectorAll<HTMLElement>('.mc-cmt-rect')]
    expect(boxes).toHaveLength(1)
    expect(boxes[0].style.left).toBe('0px')
    expect(boxes[0].style.width).toBe('100px')
    expect(boxes[0].style.height).toBe('12px')
  })
})

describe('InlineCommentOverlay activation', () => {
  beforeEach(() => {
    vi.spyOn(Range.prototype, 'getClientRects').mockImplementation(
      () => [{ left: 10, top: 20, width: 50, height: 10 }] as unknown as DOMRectList,
    )
  })

  it('opens the thread when a highlight box is clicked', () => {
    const onActivate = vi.fn()
    const { container } = mount({ comments: [anchored], onActivate })
    fireEvent.click(container.querySelector('.mc-cmt-rect') as HTMLElement)
    expect(onActivate).toHaveBeenCalledWith('a1')
  })

  it('opens the thread from the keyboard on Enter and Space, but not on other keys', () => {
    const onActivate = vi.fn()
    const { container } = mount({ comments: [anchored], onActivate })
    const box = container.querySelector('.mc-cmt-rect') as HTMLElement
    fireEvent.keyDown(box, { key: 'Enter' })
    fireEvent.keyDown(box, { key: ' ' })
    fireEvent.keyDown(box, { key: 'a' })
    expect(onActivate.mock.calls).toEqual([['a1'], ['a1']])
  })

  it('opens the thread when its gutter bubble is clicked', () => {
    const onActivate = vi.fn()
    const { container } = mount({ comments: [anchored], onActivate })
    const bubble = container.querySelector('.mc-cmt-bubble') as HTMLElement
    expect(bubble.textContent).toBe('1')
    fireEvent.click(bubble)
    expect(onActivate).toHaveBeenCalledWith('a1')
  })

  it('does not open the thread when the reader is dragging a selection', () => {
    const onActivate = vi.fn()
    vi.spyOn(window, 'getSelection').mockReturnValue({ isCollapsed: false } as unknown as Selection)
    const { container } = mount({ comments: [anchored], onActivate })
    fireEvent.click(container.querySelector('.mc-cmt-rect') as HTMLElement)
    expect(onActivate).not.toHaveBeenCalled()
  })
})

describe('InlineCommentOverlay sidebar scroll', () => {
  it('scrolls the body to the active anchor when the scroll nonce is bumped', () => {
    vi.spyOn(Range.prototype, 'getClientRects').mockImplementation(
      () => [{ left: 10, top: 200, width: 50, height: 10 }] as unknown as DOMRectList,
    )
    const scrollTo = vi.fn()
    const original = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollTo')
    Object.defineProperty(HTMLElement.prototype, 'scrollTo', { value: scrollTo, configurable: true, writable: true })
    try {
      mount({ comments: [anchored], activeId: 'a1', scrollNonce: 1 })
      expect(scrollTo).toHaveBeenCalledWith({ top: 104, behavior: 'smooth' })
    } finally {
      if (original) Object.defineProperty(HTMLElement.prototype, 'scrollTo', original)
      else delete (HTMLElement.prototype as Partial<HTMLElement>).scrollTo
    }
  })
})
