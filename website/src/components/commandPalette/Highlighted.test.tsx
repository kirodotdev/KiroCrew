import { describe, it, expect } from 'vitest'
import { render, cleanup } from '@testing-library/react'

import { Highlighted } from './Highlighted'

/**
 * Render-cost regression tests for the palette's match highlighter.
 *
 * The palette re-renders its whole result list on every selection change, and
 * hovering a row selects it — so plain mouse movement across the list rebuilds
 * every row. Each row renders a title AND a match-centered snippet through
 * {@link Highlighted}, so this component's node count per row is what decides
 * whether the list is smooth or drops frames.
 *
 * The assertions count DOM nodes rather than measuring elapsed time: node count
 * is the thing actually being controlled, and it is identical on every host,
 * whereas a wall-clock budget would measure CI load instead. A regression to a
 * per-character tree fails these immediately and unambiguously.
 */

/** Every node the component produced, text nodes included. */
function countNodes(container: HTMLElement): number {
  let n = 0
  const walk = (node: Node) => {
    n++
    node.childNodes.forEach(walk)
  }
  container.childNodes.forEach(walk)
  return n
}

describe('Highlighted render cost', () => {
  it('is bounded by the number of match runs, not by text length', () => {
    // A realistic worst case: a 200-char snippet (the server caps snippets at
    // 200) with one contiguous 6-char match — what a substring hit looks like.
    const text = 'x'.repeat(97) + 'needle' + 'y'.repeat(97)
    const indices = Array.from({ length: 6 }, (_, i) => 97 + i)

    const { container } = render(<Highlighted text={text} indices={indices} />)

    // 3 runs (before / match / after) => 2 text nodes + 1 <strong> + its text.
    // A per-character implementation produces 400+.
    expect(countNodes(container)).toBeLessThanOrEqual(8)
    expect(container.querySelectorAll('strong')).toHaveLength(1)
    cleanup()
  })

  it('stays bounded for a scattered fuzzy match', () => {
    // Fuzzy matching scatters single characters — the pathological input for a
    // run-based encoder. Even here the count tracks match COUNT, not length.
    const text = 'deployment pipeline configuration for the staging environment'
    const indices = [0, 11, 20, 30, 40, 50]

    const { container } = render(<Highlighted text={text} indices={indices} />)

    expect(container.querySelectorAll('strong')).toHaveLength(6)
    // 6 matched runs + at most 7 gaps = 13 runs; each <strong> also carries one
    // child text node.
    expect(countNodes(container)).toBeLessThanOrEqual(20)
    cleanup()
  })

  it('renders a long unmatched title as a single text node', () => {
    const { container } = render(<Highlighted text={'z'.repeat(500)} indices={[]} />)
    expect(countNodes(container)).toBe(1)
    cleanup()
  })
})

describe('Highlighted correctness', () => {
  it('emphasises exactly the requested characters and nothing else', () => {
    const { container } = render(<Highlighted text="abcdef" indices={[1, 2, 4]} />)

    expect(container.textContent).toBe('abcdef')
    const marks = Array.from(container.querySelectorAll('strong')).map((e) => e.textContent)
    expect(marks).toEqual(['bc', 'e'])
    cleanup()
  })

  it('handles a match at both boundaries', () => {
    const { container } = render(<Highlighted text="abc" indices={[0, 2]} />)
    expect(container.textContent).toBe('abc')
    expect(Array.from(container.querySelectorAll('strong')).map((e) => e.textContent)).toEqual([
      'a',
      'c',
    ])
    cleanup()
  })

  it('emphasises the whole string when every character matches', () => {
    const { container } = render(<Highlighted text="abc" indices={[0, 1, 2]} />)
    expect(Array.from(container.querySelectorAll('strong')).map((e) => e.textContent)).toEqual([
      'abc',
    ])
    cleanup()
  })

  it('ignores out-of-range and duplicated indices', () => {
    const { container } = render(<Highlighted text="abc" indices={[1, 1, 99, -1]} />)
    expect(container.textContent).toBe('abc')
    expect(Array.from(container.querySelectorAll('strong')).map((e) => e.textContent)).toEqual([
      'b',
    ])
    cleanup()
  })

  it('preserves the text verbatim for non-ASCII characters', () => {
    const { container } = render(<Highlighted text="поиск" indices={[1, 2]} />)
    expect(container.textContent).toBe('поиск')
    expect(Array.from(container.querySelectorAll('strong')).map((e) => e.textContent)).toEqual([
      'ои',
    ])
    cleanup()
  })
})
