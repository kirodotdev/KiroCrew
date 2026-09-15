import { describe, it, expect, beforeEach } from 'vitest'
import { render, waitFor } from '@testing-library/react'

import MarkdownRenderer from '../components/MarkdownRenderer'
import { __resetRendererCache } from '../components/ExcalidrawBlock'
import { parseBlocks } from '../hooks/useBlockAssembler'

const SCENE = JSON.stringify({
  type: 'excalidraw',
  version: 2,
  elements: [
    {
      type: 'rectangle', x: 0, y: 0, width: 120, height: 60,
      strokeColor: '#1e1e1e', backgroundColor: 'transparent',
      strokeWidth: 1, strokeStyle: 'solid', roughness: 1, opacity: 100, seed: 7,
    },
    {
      type: 'text', x: 12, y: 20, width: 96, height: 25, text: 'hello box',
      strokeColor: '#1e1e1e', fontSize: 20, fontFamily: 1, textAlign: 'left',
      opacity: 100, seed: 8,
    },
  ],
})

const fence = (body: string) => '```excalidraw\n' + body + '\n```'

// The rendered diagram, specifically. A bare `svg` selector would also match
// Lucide icons in surrounding chrome (CodeBlock's toolbar ships one), and `role`
// is only set when the scene has text to name it with.
const DIAGRAM = 'svg[data-excalidraw-scene]'

describe('excalidraw fenced blocks in chat', () => {
  beforeEach(() => { __resetRendererCache() })

  it('renders the scene as inline SVG instead of printing the JSON', async () => {
    const { container } = render(<MarkdownRenderer content={fence(SCENE)} />)
    // Inline SVG (not an iframe) is the point: it reflows with the chat column
    // and stays selectable.
    await waitFor(() => expect(container.querySelector(DIAGRAM)).not.toBeNull())
    expect(container.querySelector('iframe')).toBeNull()
    const text = Array.from(container.querySelectorAll('text')).map(t => t.textContent)
    expect(text).toContain('hello box')
  })

  it('explains itself and shows the source when the scene is malformed', async () => {
    // A bare wall of red JSON reads as a crash rather than a fallback, so the
    // failure names what happened and keeps the source muted and capped.
    const broken = '{"elements": [ oops'
    const { container } = render(<MarkdownRenderer content={fence(broken)} />)
    await waitFor(() => {
      const pre = container.querySelector('pre')
      expect(pre?.textContent).toContain('oops')
    })
    expect(container.textContent).toContain("Couldn't render diagram")
    const pre = container.querySelector('pre')!
    expect(pre.className).toContain('text-muted')
    expect(pre.className).not.toContain('text-danger')
    expect(container.querySelector(DIAGRAM)).toBeNull()
  })

  it('leaves other fence languages alone', async () => {
    const { container } = render(
      <MarkdownRenderer content={'```json\n{"elements":[]}\n```'} />,
    )
    await waitFor(() => expect(container.textContent).toContain('elements'))
    expect(container.querySelector(DIAGRAM)).toBeNull()
  })
})

describe('parseBlocks excalidraw classification', () => {
  it('classifies a closed excalidraw fence as an excalidraw block', () => {
    const blocks = parseBlocks(fence(SCENE), false)
    expect(blocks).toHaveLength(1)
    expect(blocks[0].type).toBe('excalidraw')
    expect(blocks[0].complete).toBe(true)
  })

  it('marks an unclosed fence incomplete while streaming', () => {
    // A half-arrived scene is invalid JSON, so the renderer must not be handed
    // it — the block stays incomplete and the caller shows a placeholder.
    const blocks = parseBlocks('```excalidraw\n{"elements":[', true)
    expect(blocks[0].type).toBe('excalidraw')
    expect(blocks[0].complete).toBe(false)
  })

  it('wins over the diff content heuristic', () => {
    // An explicit fence language must never lose to content sniffing.
    const blocks = parseBlocks('```excalidraw\n+1: a\n+2: b\n```', false)
    expect(blocks[0].type).toBe('excalidraw')
  })

  it('still classifies mermaid fences as mermaid', () => {
    const blocks = parseBlocks('```mermaid\ngraph TD;A-->B\n```', false)
    expect(blocks[0].type).toBe('mermaid')
  })
})
