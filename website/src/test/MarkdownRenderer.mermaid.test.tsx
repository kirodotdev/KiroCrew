import { describe, it, expect, vi } from 'vitest'
import { render } from '@testing-library/react'

vi.mock('mermaid', () => ({
  default: {
    initialize: vi.fn(),
    render: vi.fn().mockResolvedValue({ svg: '<svg></svg>' }),
  },
}))

import mermaid from 'mermaid'
import MarkdownRenderer from '../components/MarkdownRenderer'

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
    await vi.waitFor(() =>
      expect(mermaid.initialize).toHaveBeenCalledWith(
        expect.objectContaining({ suppressErrorRendering: true })
      )
    )
  })
})
