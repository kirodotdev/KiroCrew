import { describe, it, expect, beforeAll, afterEach } from 'vitest'
import { render, cleanup } from '@testing-library/react'
import { useEffect, useRef, type ReactNode } from 'react'
import { applySearchHighlights, clearSearchHighlights } from '../utils/domHighlight'
import { installHighlightApiStub } from './highlightApiStub'

/**
 * The transcript is React-reconciled. A highlight that splits a React-owned text
 * node into pieces (or merges pieces back with normalize()) leaves React's fiber
 * pointing at a node that is not where React put it; the next commit that
 * deletes that node throws NotFoundError ("The node to be removed is not a child
 * of this node"), which MessageErrorBoundary surfaces as message_render, and a
 * commit that rewrites it updates a detached node so the visible text goes stale.
 *
 * Every paragraph here gives the text a sibling element, so React mounts a real
 * text fiber for it (a lone string child is written through `textContent` and
 * never removed one node at a time). These are the commits a streaming or
 * settling message performs while the find bar is open.
 */

beforeAll(() => { installHighlightApiStub() })
afterEach(() => cleanup())

function Bubble({ children, term, occ = 0 }: { children: ReactNode; term: string; occ?: number }) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    applySearchHighlights(el, term, false, occ)
    return () => clearSearchHighlights(el)
  })
  return <div ref={ref}>{children}</div>
}

describe('search highlights survive React reconciliation', () => {
  it('a commit that deletes a highlighted text node does not throw', () => {
    const { rerender } = render(<Bubble term="world"><p>{'hello world'}<em>!</em></p></Bubble>)
    // The text fiber is dropped: React removes the node it mounted, which must
    // still be where React left it.
    expect(() => rerender(<Bubble term="world"><p><em>!</em></p></Bubble>)).not.toThrow()
  })

  it('a commit that rewrites a highlighted text node keeps the visible text current', () => {
    const { container, rerender } = render(<Bubble term="foo"><p>{'foo bar'}<em>!</em></p></Bubble>)
    rerender(<Bubble term="foo"><p>{'foo bar baz'}<em>!</em></p></Bubble>)
    expect(container.querySelector('p')!.textContent).toBe('foo bar baz!')
  })

  it('the streaming glow shape (text + trailing span) can collapse back to one text node', () => {
    // Mirrors rehypeStreamingGlow dropping out when a stream ends: the trailing
    // <span> and the leading text fiber are both deleted and the paragraph is
    // rewritten as one string.
    const { container, rerender } = render(
      <Bubble term="set"><p>{'set the '}<span className="streaming-glow">ramp to 25%</span></p></Bubble>,
    )
    expect(() => rerender(<Bubble term="set"><p>{'set the ramp to 25%'}</p></Bubble>)).not.toThrow()
    expect(container.querySelector('p')!.textContent).toBe('set the ramp to 25%')
  })

  it('closing the find bar (empty term) and re-rendering does not throw', () => {
    const { rerender } = render(<Bubble term="a"><p>{'a b a'}<em>!</em></p></Bubble>)
    rerender(<Bubble term=""><p>{'a b a'}<em>!</em></p></Bubble>)
    expect(() => rerender(<Bubble term=""><p><em>!</em></p></Bubble>)).not.toThrow()
  })

  it('unmounting a highlighted message does not throw', () => {
    const { unmount } = render(<Bubble term="x"><p>{'x y x'}<em>!</em></p></Bubble>)
    expect(() => unmount()).not.toThrow()
  })
})
