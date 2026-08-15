import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useBlockAssembler, parseBlocks } from '../hooks/useBlockAssembler'
import type { ContentBlock } from '../types'

// Mirrors THROTTLE_MS in the hook. Every wait below goes through fake timers,
// so no assertion depends on wall-clock progress or host load.
const THROTTLE_MS = 100

// An export spy cannot see the hook's intra-module call. parseBlocks opens with
// one `raw.split('\n')`, so counting that on a marked input counts real parses.
type SplitFn = typeof String.prototype.split
const realSplit: SplitFn = String.prototype.split
const MARK = 'prk-marked-input'

function countParsesOf(prefix: string): () => number {
  let n = 0
  const patched = function (this: string, ...args: Parameters<SplitFn>): string[] {
    if (args[0] === '\n' && this.startsWith(prefix)) n += 1
    return realSplit.apply(this, args)
  }
  String.prototype.split = patched as unknown as SplitFn
  return () => n
}

describe('useBlockAssembler streaming throttle', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => {
    String.prototype.split = realSplit
    vi.useRealTimers()
  })

  it('parses at most once per throttle window while streaming, not once per chunk', () => {
    const chunks = [1, 2, 3, 4, 5, 6].map(n => `${MARK} chunk ${'x'.repeat(n)}`)
    const parses = countParsesOf(MARK)

    const { rerender } = renderHook(
      ({ text }) => useBlockAssembler(text, true),
      { initialProps: { text: chunks[0] } },
    )
    const afterMount = parses()
    expect(afterMount).toBe(1)

    for (const text of chunks.slice(1)) rerender({ text })
    // Five further chunks arrived inside one window and none of them parsed.
    expect(parses()).toBe(afterMount)

    act(() => { vi.advanceTimersByTime(THROTTLE_MS) })
    expect(parses()).toBe(afterMount + 1)

    // A second window costs exactly one more parse, however many chunks land.
    rerender({ text: `${MARK} chunk next` })
    rerender({ text: `${MARK} chunk next+1` })
    expect(parses()).toBe(afterMount + 1)
    act(() => { vi.advanceTimersByTime(THROTTLE_MS) })
    expect(parses()).toBe(afterMount + 2)
  })

  it('parses once on a cold mount of a completed message, not twice', () => {
    // Only a fresh mount runs the useState seed, so a rerender into
    // streaming:false cannot reach it -- this has to mount false outright.
    const text = `${MARK} done\n\`\`\`js\nconst x = 1\n\`\`\`\ntail`
    const parses = countParsesOf(MARK)

    const { result } = renderHook(() => useBlockAssembler(text, false))
    // Read before the assertion below, which parses again on the same counter.
    const afterMount = parses()

    expect(afterMount).toBe(1)
    expect(result.current).toEqual(parseBlocks(text, false))
  })

  it('returns a stable reference across renders inside one window', () => {
    const { result, rerender } = renderHook(
      ({ text }) => useBlockAssembler(text, true),
      { initialProps: { text: 'alpha' } },
    )
    const first = result.current
    rerender({ text: 'alpha beta' })
    rerender({ text: 'alpha beta gamma' })
    expect(result.current).toBe(first)
  })

  it('holds the stale parse inside a window and lands an exact parse when streaming ends', () => {
    const opening = 'intro\n```js\nconst x = 1'
    const closed = 'intro\n```js\nconst x = 1\n```\noutro'

    const { result, rerender } = renderHook(
      ({ text, streaming }) => useBlockAssembler(text, streaming),
      { initialProps: { text: opening, streaming: true } },
    )
    act(() => { vi.advanceTimersByTime(THROTTLE_MS) })
    expect(result.current).toEqual(parseBlocks(opening, true))

    rerender({ text: closed, streaming: true })
    // Pins that the throttle defers: a settled result is identical either way,
    // so only the moment of the write separates throttled from unthrottled.
    expect(result.current).toEqual(parseBlocks(opening, true))
    expect(result.current).not.toEqual(parseBlocks(closed, true))

    rerender({ text: closed, streaming: false })
    expect(result.current).toEqual(parseBlocks(closed, false))
  })

  it('final output is identical to an unthrottled parse for representative inputs', () => {
    const inputs = [
      'Hello **world**',
      '```js\nconst x = 1\n```',
      'before\n```python\ndef foo():\n  pass\n```\nafter',
      '```\n@@ -1,3 +1,4 @@\n-old\n+new\n```',
      '```mermaid\ngraph TD\nA-->B\n```',
      'text\n<mcwidget title="T" slug="foo">body</mcwidget>\nafter',
      'lead\n```markdown\nnested ```py\nx\n```\n```\ntail',
    ]

    for (const full of inputs) {
      const half = full.slice(0, Math.max(1, Math.floor(full.length / 2)))
      const { result, rerender } = renderHook(
        ({ text, streaming }) => useBlockAssembler(text, streaming),
        { initialProps: { text: half, streaming: true } },
      )
      rerender({ text: full, streaming: true })
      act(() => { vi.advanceTimersByTime(THROTTLE_MS) })
      rerender({ text: full, streaming: false })
      expect(result.current).toEqual(parseBlocks(full, false))
    }
  })

  it('does not parse after unmount', () => {
    const parses = countParsesOf(MARK)
    const { rerender, unmount } = renderHook(
      ({ text }) => useBlockAssembler(text, true),
      { initialProps: { text: `${MARK} one` } },
    )
    rerender({ text: `${MARK} one two` })
    const before = parses()

    unmount()
    act(() => { vi.advanceTimersByTime(THROTTLE_MS * 5) })
    expect(parses()).toBe(before)
  })

  it('lands the exact parse in the same render streaming ends, not a commit later', () => {
    const opening = 'intro\n```js\nconst x = 1'
    const closed = 'intro\n```js\nconst x = 1\n```\noutro'
    // Recorded during render: result.current would let a stale first render
    // hide behind the effect-driven re-render act() flushes, a visible flash.
    const rendered: ContentBlock[][] = []

    const { rerender } = renderHook(
      ({ text, streaming }) => {
        const blocks = useBlockAssembler(text, streaming)
        rendered.push(blocks)
        return blocks
      },
      { initialProps: { text: opening, streaming: true } },
    )
    act(() => { vi.advanceTimersByTime(THROTTLE_MS) })
    rerender({ text: closed, streaming: true })

    const firstAfterEnd = rendered.length
    rerender({ text: closed, streaming: false })
    expect(rendered[firstAfterEnd]).toEqual(parseBlocks(closed, false))
  })
})
