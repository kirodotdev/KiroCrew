import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import AssistantMessage from '../pages/chat/AssistantMessage'

// useSmoothStream's rAF loop is not needed to render a finished reply.
vi.mock('../hooks/useSmoothStream', () => ({
  useSmoothStream: (content: string) => content,
}))

afterEach(() => { cleanup(); document.getSelection()?.removeAllRanges() })

function selectBetween(start: Node, startOffset: number, end: Node, endOffset: number) {
  const range = document.createRange()
  range.setStart(start, startOffset)
  range.setEnd(end, endOffset)
  const sel = document.getSelection()!
  sel.removeAllRanges()
  sel.addRange(range)
}

function textNode(root: Element, needle: string): Text {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if ((n.nodeValue ?? '').includes(needle)) return n as Text
  }
  throw new Error(`no text node contains ${needle}`)
}

/** Fire a copy on `target` and return what the handler wrote. */
function copyFrom(target: Element) {
  const data = new Map<string, string>()
  const clipboardData = { setData: (t: string, v: string) => { data.set(t, v) }, getData: (t: string) => data.get(t) ?? '' }
  const notCancelled = fireEvent.copy(target, { clipboardData })
  return { data, cancelled: !notCancelled }
}

describe('AssistantMessage: copying a selection keeps markdown', () => {
  it('writes markdown and sanitized html for a formatted selection', () => {
    render(<AssistantMessage content={'## Result\n\nSee **the fix** in [the docs](https://example.com/guide).\n\nDone.'} isStreaming={false} slotRunning={false} />)
    const bubble = screen.getByTestId('message-bubble')
    selectBetween(textNode(bubble, 'Result'), 0, textNode(bubble, 'Done.'), 5)
    const { data, cancelled } = copyFrom(bubble)
    expect(cancelled).toBe(true)
    expect(data.get('text/plain')).toBe('## Result\n\nSee **the fix** in [the docs](https://example.com/guide).\n\nDone.')
    const html = data.get('text/html') ?? ''
    expect(html).toContain('<strong>the fix</strong>')
    expect(html).toContain('<a href="https://example.com/guide">the docs</a>')
    expect(html).not.toMatch(/class=|style=|on\w+=/)
  })

  it('writes a pull-request chip as a link labelled with its reference', () => {
    render(<AssistantMessage content={'Opened [the PR](https://github.com/o/r/pull/1) **today**'} isStreaming={false} slotRunning={false} />)
    const bubble = screen.getByTestId('message-bubble')
    selectBetween(textNode(bubble, 'Opened'), 0, textNode(bubble, 'today'), 5)
    expect(copyFrom(bubble).data.get('text/plain')).toBe('Opened [o/r#1](https://github.com/o/r/pull/1) **today**')
  })

  it('writes inline code and a file-path chip as code', () => {
    render(<AssistantMessage content={'Run `npm test` in `/tmp/x.ts` **now**'} isStreaming={false} slotRunning={false} />)
    const bubble = screen.getByTestId('message-bubble')
    selectBetween(textNode(bubble, 'Run'), 0, textNode(bubble, 'now'), 3)
    expect(copyFrom(bubble).data.get('text/plain')).toBe('Run `npm test` in `/tmp/x.ts` **now**')
  })

  it('writes plain prose as its text', () => {
    render(<AssistantMessage content="just words here" isStreaming={false} slotRunning={false} />)
    const bubble = screen.getByTestId('message-bubble')
    const text = textNode(bubble, 'just')
    selectBetween(text, 0, text, 10)
    const { data, cancelled } = copyFrom(bubble)
    expect(cancelled).toBe(true)
    expect(data.get('text/plain')).toBe('just words')
    expect(data.get('text/html')).toBe('just words')
  })

  it('flattens a list to its items as paragraphs', () => {
    render(<AssistantMessage content={'**Intro**\n\n- one\n- two'} isStreaming={false} slotRunning={false} />)
    const bubble = screen.getByTestId('message-bubble')
    selectBetween(textNode(bubble, 'Intro'), 0, textNode(bubble, 'two'), 3)
    const { data, cancelled } = copyFrom(bubble)
    expect(cancelled).toBe(true)
    expect(data.get('text/plain')).toBe('**Intro**\n\none\n\ntwo')
  })
})

describe('AssistantMessage: silent fallback when nothing serializes', () => {
  it('leaves a collapsed selection to the browser', () => {
    render(<AssistantMessage content={'See **the fix** now'} isStreaming={false} slotRunning={false} />)
    const bubble = screen.getByTestId('message-bubble')
    const text = textNode(bubble, 'the fix')
    selectBetween(text, 2, text, 2)
    const { data, cancelled } = copyFrom(bubble)
    expect(cancelled).toBe(false)
    expect(data.size).toBe(0)
  })

  it('leaves a selection outside the bubble to the browser', () => {
    render(<AssistantMessage content={'See **the fix** now'} isStreaming={false} slotRunning={false} />)
    const bubble = screen.getByTestId('message-bubble')
    const outside = document.createElement('p')
    outside.textContent = 'elsewhere'
    document.body.appendChild(outside)
    selectBetween(outside.firstChild!, 0, outside.firstChild!, 4)
    const { data, cancelled } = copyFrom(bubble)
    expect(cancelled).toBe(false)
    expect(data.size).toBe(0)
    outside.remove()
  })
})
