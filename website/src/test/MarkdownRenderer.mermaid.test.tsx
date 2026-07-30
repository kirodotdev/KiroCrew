import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor } from '@testing-library/react'

vi.mock('mermaid', () => ({
  default: {
    initialize: vi.fn(),
    render: vi.fn().mockResolvedValue({ svg: '<svg></svg>' }),
  },
}))

import mermaid from 'mermaid'
import MarkdownRenderer from '../components/MarkdownRenderer'

beforeEach(() => { vi.clearAllMocks() })

describe('MarkdownRenderer mermaid config', () => {
  it('initializes mermaid with suppressErrorRendering so parse errors do not leak error SVGs into the DOM', async () => {
    // Regression: without suppressErrorRendering, a mermaid parse error injects a
    // temp <div id="dmermaid-*"> into document.body that render() never cleans up
    // (cleanup only runs on success), accumulating orphaned error graphics.
    render(<MarkdownRenderer content={'```mermaid\ngraph TD;A-->B\n```'} />)
    await waitFor(() => {
      expect(mermaid.initialize).toHaveBeenCalledWith(
        expect.objectContaining({ suppressErrorRendering: true })
      )
    })
  })

  it('renders the diagram through the lazily-imported module', async () => {
    render(<MarkdownRenderer content={'```mermaid\ngraph TD;A-->B\n```'} />)
    await waitFor(() => expect(mermaid.render).toHaveBeenCalled())
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
