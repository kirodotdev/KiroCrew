import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import AssistantMessage from '../pages/chat/AssistantMessage'
import { extractInlineThinking } from '../app-sdk/protocol/inlineThinking'
import { deriveFollowUpOptions, type ChatMessage } from '../app-sdk/protocol'
import { createTestStore, renderWithProviders } from './helpers'

vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))
vi.mock('../hooks/useSmoothStream', () => ({ useSmoothStream: (content: string) => content }))
vi.mock('../utils/clipboard', () => ({
  copyToClipboard: vi.fn().mockResolvedValue(true),
  copyRichToClipboard: vi.fn().mockResolvedValue(true),
}))
import { copyToClipboard } from '../utils/clipboard'

const SCRATCH = 'The user wants <ul><li>one</li></ul> and **two**.'
const REPLY = `<thinking>\n${SCRATCH}\n</thinking>\n\nHere is the answer.`

// A model with no separate reasoning stream writes `<thinking>` into the reply
// text itself (#30567). The bubble must show the answer, and the reasoning only
// behind the collapsed row the streamed kind uses.
describe('AssistantMessage inline <thinking>', () => {
  it('draws the reasoning row and keeps the block out of the bubble', () => {
    render(<AssistantMessage content={REPLY} isStreaming={false} slotRunning={false} />)
    expect(screen.getByTestId('md').textContent).toBe('Here is the answer.')
    const toggle = screen.getByRole('button', { name: 'Expand model reasoning' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(toggle)
    expect(screen.getByTestId('thinking-body').textContent).toBe(SCRATCH)
  })

  it('copies the answer without the scratchpad', async () => {
    render(<AssistantMessage content={REPLY} isStreaming={false} slotRunning={false} onQuoteMessage={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    await act(async () => {})
    expect(copyToClipboard).toHaveBeenCalledWith('Here is the answer.')
  })

  it('hides a block still streaming, before its closer arrives', () => {
    render(<AssistantMessage content={'<thinking>\nhalf a tho'} isStreaming slotRunning />)
    expect(screen.getByTestId('md').textContent).toBe('')
  })

  it('reads the answer aloud without the scratchpad', () => {
    const onSpeak = vi.fn()
    renderWithProviders(
      <AssistantMessage content={REPLY} isStreaming={false} slotRunning={false} onSpeak={onSpeak} />,
      { store: createTestStore() },
    )
    fireEvent.pointerDown(screen.getByTitle('More actions'), { button: 0, ctrlKey: false, pointerType: 'mouse' })
    fireEvent.click(screen.getByTestId('speak-message'))
    expect(onSpeak).toHaveBeenCalledWith('Here is the answer.')
  })

  it('offers no chips for a marker that sits inside the block', () => {
    const msgs = [
      { role: 'user', content: 'go', ts: 1 },
      { role: 'assistant', content: '<thinking>\nI could end with [OPTIONS: A | B]\n</thinking>\nDone.', ts: 2 },
    ] as ChatMessage[]
    expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual([])
  })

  it('draws no row for a reply without one', () => {
    render(<AssistantMessage content="Plain answer." isStreaming={false} slotRunning={false} />)
    expect(screen.queryByRole('button', { name: 'Expand model reasoning' })).toBeNull()
    expect(screen.getByTestId('md').textContent).toBe('Plain answer.')
  })
})

describe('extractInlineThinking', () => {
  it('returns text with no block unchanged', () => {
    const t = 'Hello\n\n  world  '
    expect(extractInlineThinking(t)).toEqual({ cleaned: t, thinking: '' })
  })

  it('splits a leading block and the namespaced spelling', () => {
    expect(extractInlineThinking('<thinking>plan</thinking>\nAnswer')).toEqual({ cleaned: 'Answer', thinking: 'plan' })
    const ns = ['antml', 'thinking'].join(':')
    expect(extractInlineThinking(`<${ns}>plan</${ns}>\nAnswer`)).toEqual({ cleaned: 'Answer', thinking: 'plan' })
  })

  it('joins several blocks and closes the gap each leaves', () => {
    const r = extractInlineThinking('<thinking>a</thinking>\n\nOne.\n\n<thinking>b</thinking>\n\nTwo.')
    expect(r.thinking).toBe('a\n\nb')
    expect(r.cleaned).toBe('One.\n\nTwo.')
    expect(extractInlineThinking('<thinking>a</thinking>\n<thinking>b</thinking>\nThree.')).toEqual({ cleaned: 'Three.', thinking: 'a\n\nb' })
  })

  it('leaves a tag mentioned in prose or inline code', () => {
    const t = 'Wrap it in `<thinking>x</thinking>` tags, or write <thinking>y</thinking> inline.'
    expect(extractInlineThinking(t)).toEqual({ cleaned: t, thinking: '' })
  })

  it('leaves an example inside a fenced code block', () => {
    const t = 'Example:\n\n```xml\n<thinking>\nstep one\n</thinking>\n```\n'
    expect(extractInlineThinking(t)).toEqual({ cleaned: t, thinking: '' })
  })

  it('still splits a real block after a closed fence', () => {
    const t = '```\ncode\n```\n<thinking>why</thinking>\nDone.'
    expect(extractInlineThinking(t)).toEqual({ cleaned: '```\ncode\n```\nDone.', thinking: 'why' })
  })

  it('keeps the answer\'s own blank lines and indentation byte for byte', () => {
    const code = '```python\nimport os\n\n\ndef f():\n    pass\n```'
    expect(extractInlineThinking(`<thinking>plan</thinking>\n${code}`).cleaned).toBe(code)
    expect(extractInlineThinking('<thinking>plan</thinking>\n\n    indented code\nprose').cleaned).toBe('    indented code\nprose')
  })

  it('leaves an opener indented four spaces, which is code', () => {
    const t = '- Example:\n    ```xml\n    <thinking>\n    x\n    </thinking>\n    ```'
    expect(extractInlineThinking(t)).toEqual({ cleaned: t, thinking: '' })
    const four = 'Intro\n\n    <thinking>x</thinking>\nafter'
    expect(extractInlineThinking(four)).toEqual({ cleaned: four, thinking: '' })
  })

  it('splits an opener after up to three spaces, and leaves one after a tab', () => {
    expect(extractInlineThinking('   <thinking>x</thinking>\nAnswer')).toEqual({ cleaned: 'Answer', thinking: 'x' })
    const tab = '\t<thinking>x</thinking>\nAnswer'
    expect(extractInlineThinking(tab)).toEqual({ cleaned: tab, thinking: '' })
  })

  it('reads text after a closer on its line as answer, not as a new line start', () => {
    const r = extractInlineThinking('<thinking>a</thinking> then <thinking>b</thinking>\nEnd')
    expect(r).toEqual({ cleaned: ' then <thinking>b</thinking>\nEnd', thinking: 'a' })
  })

  it('ignores a fence-like line inside the block when tracking later fences', () => {
    const t = '<thinking>\n```\n</thinking>\nAnswer\n<thinking>z</thinking>\nEnd'
    expect(extractInlineThinking(t)).toEqual({ cleaned: 'Answer\nEnd', thinking: '```\n\nz' })
  })

  it('keeps an unclosed opener in a finished reply, and hides it while streaming', () => {
    const t = 'Answer first.\n<thinking>never closed'
    expect(extractInlineThinking(t)).toEqual({ cleaned: t, thinking: '' })
    expect(extractInlineThinking(t, { hidePartial: true })).toEqual({ cleaned: 'Answer first.\n', thinking: 'never closed' })
  })
})
