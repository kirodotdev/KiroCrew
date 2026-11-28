import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import AssistantMessage from '../pages/chat/AssistantMessage'

// The renderer is replaced so the test reads exactly what AssistantMessage hands
// it: the revealed text and whether that text is still arriving.
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content, streaming }: { content: string; streaming?: boolean }) => (
    <div data-testid="md" data-streaming={String(!!streaming)}>{content}</div>
  ),
}))
// The smooth reveal is held at a fixed number of characters, which is the state
// its post-stream drain leaves the reply in for a frame (or, in a hidden tab, for
// as long as the tab stays hidden).
const reveal = vi.hoisted(() => ({ len: Infinity }))
vi.mock('../hooks/useSmoothStream', () => ({
  useSmoothStream: (content: string) => content.slice(0, reveal.len),
}))

afterEach(() => {
  cleanup()
  reveal.len = Infinity
})

const WIDGET = '<mcwidget title="Fix diagram">\n<div><h3>Diagram</h3><p>body</p></div>\n</mcwidget>'
const REPLY = `The diagram below shows the change.\n\n${WIDGET}\n\nThat is all.`

describe('AssistantMessage post-stream drain', () => {
  it('tells the renderer a reply is still streaming while its reveal is behind the finished text', () => {
    // Cut inside the widget body: parsed as finished text, this is a complete
    // widget whose body grows every frame, and each body mints its own sandbox
    // document: about 280 mints for one widget, and past 64 the gateway answers 503.
    reveal.len = REPLY.indexOf('<p>body')
    render(<AssistantMessage content={REPLY} isStreaming={false} slotRunning={false} />)
    const md = screen.getByTestId('md')
    expect(md.textContent).toBe(REPLY.slice(0, reveal.len))
    expect(md).toHaveAttribute('data-streaming', 'true')
  })

  it('hands the renderer finished text once the reveal has caught up', () => {
    render(<AssistantMessage content={REPLY} isStreaming={false} slotRunning={false} />)
    const md = screen.getByTestId('md')
    expect(md.textContent).toBe(REPLY)
    expect(md).toHaveAttribute('data-streaming', 'false')
  })
})
