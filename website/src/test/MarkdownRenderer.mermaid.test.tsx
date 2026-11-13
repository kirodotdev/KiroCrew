import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import { namedCeiling } from './namedCeiling'

vi.mock('mermaid', () => ({
  default: {
    initialize: vi.fn(),
    render: vi.fn().mockResolvedValue({ svg: '<svg></svg>' }),
  },
}))

import mermaid from 'mermaid'
import MarkdownRenderer from '../components/MarkdownRenderer'

beforeEach(() => { vi.clearAllMocks() })

// Chain: MermaidBlock.draw() -> dynamic import('mermaid') -> render -> SVG install. A wait behind that lazy
// boundary carries an explicit ceiling instead of the 1000 ms default.
const MERMAID_DRAWN = namedCeiling('MERMAID_DRAWN', 5000)

describe('MarkdownRenderer mermaid config', () => {
  it('initializes mermaid with suppressErrorRendering so parse errors do not leak error SVGs into the DOM', async () => {
    // Regression: without suppressErrorRendering, a mermaid parse error injects a
    // temp <div id="dmermaid-*"> into document.body that render() never cleans up
    // (cleanup only runs on success), accumulating orphaned error graphics.
    //
    // Awaited because mermaid is loaded by `import()` inside MermaidBlock
    // (it is ~90-130 KB gzip and must stay off the critical path), so
    // initialize() lands a microtask after render rather than during it.
    render(<MarkdownRenderer content={'```mermaid\ngraph TD;A-->B\n```'} />)
    await waitFor(() =>
      expect(mermaid.initialize).toHaveBeenCalledWith(
        expect.objectContaining({ suppressErrorRendering: true })
      ), MERMAID_DRAWN)
  })

  it('renders the diagram through the lazily-imported module', async () => {
    render(<MarkdownRenderer content={'```mermaid\ngraph TD;A-->B\n```'} />)
    await waitFor(() => expect(mermaid.render).toHaveBeenCalled(), MERMAID_DRAWN)
  })

  it('does NOT touch mermaid for content without a diagram', async () => {
    // The point of the dynamic import: mermaid must not be pulled in — nor
    // initialized — just because a chat message rendered.
    render(<MarkdownRenderer content={'# Hello\n\nplain text and `code`'} />)
    await new Promise(r => setTimeout(r, 50))
    expect(mermaid.initialize).not.toHaveBeenCalled()
    expect(mermaid.render).not.toHaveBeenCalled()
  })
})
