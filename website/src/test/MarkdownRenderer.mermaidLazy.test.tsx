/**
 * mermaid must stay OFF the critical path.
 *
 * MarkdownRenderer renders every chat message, so a static `import mermaid` put
 * the whole diagram engine (~90-130 KB gzip) in the entry chunk for every user
 * while a ```mermaid fence is rare. The engine is now loaded by `import()` from
 * inside `MermaidBlock`.
 *
 * The mock factory counts LOADS rather than calls: vitest invokes it the first
 * time the module is actually requested, which is import time under a static
 * import and first-diagram-render time under the dynamic one. That is exactly
 * the distinction being locked in, so a revert to the static import fails the
 * "not loaded" case before any assertion about rendering runs.
 */
import { describe, it, expect, vi } from 'vitest'
import { act, render, waitFor } from '@testing-library/react'

const probe = vi.hoisted(() => ({ loads: 0 }))

vi.mock('mermaid', () => {
  probe.loads++
  return {
    default: {
      initialize: vi.fn(),
      render: vi.fn().mockResolvedValue({ svg: '<svg data-testid="diagram"></svg>' }),
    },
  }
})

import MarkdownRenderer from '../components/MarkdownRenderer'
import { namedCeiling } from './namedCeiling'

describe('mermaid is loaded on demand', () => {
  it('is not loaded by importing the renderer', () => {
    // The import above already ran. A static import would have pulled mermaid
    // in with it, before any component mounted.
    expect(probe.loads).toBe(0)
  })

  it('is not loaded by markdown without a diagram', async () => {
    render(<MarkdownRenderer content={'plain **text** and `code`'} />)
    await Promise.resolve()
    expect(probe.loads).toBe(0)
  })

  it('is loaded exactly once when diagram fences render', async () => {
    // Two fences in one document: the load happens (on demand) and happens
    // ONCE — the cached module promise, and `import()` itself, dedupe it.
    // Cases above must run first; the cache is module-scoped for the file.
    const { container } = render(<MarkdownRenderer content={'```mermaid\ngraph TD;A-->B\n```\n\n```mermaid\ngraph TD;C-->D\n```'} />)
    // The load and both draws resolve after render returns. The draw inserts
    // each SVG and then stores it in state, so waiting for the SVGs inside act
    // keeps that state update in the same act scope.
    await act(async () => {
      // Chain: MermaidBlock.draw() -> dynamic import('mermaid') -> render -> SVG install. Both waits sit behind that
      // lazy boundary, so each carries an explicit ceiling instead of the 1000 ms default.
      await vi.waitFor(() => expect(probe.loads).toBe(1), { timeout: 5000 })
      await waitFor(() => expect(container.querySelectorAll('[data-testid="diagram"]')).toHaveLength(2),
        namedCeiling('MERMAID_LAZY_DRAWS', 5000))
    })
    expect(probe.loads).toBe(1)
  })
})
