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

let origRaf: any
let origCaf: any

beforeEach(() => {
  (globalThis as any).ResizeObserver = FakeRO
  origRaf = globalThis.requestAnimationFrame
  origCaf = globalThis.cancelAnimationFrame
  globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { cb(0); return 1 }) as any
  globalThis.cancelAnimationFrame = (() => {}) as any
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
})
