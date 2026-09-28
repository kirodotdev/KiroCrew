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

// The hook's newline scan (nextLineOf) is the one place that reads the whole
// accumulated text one code unit at a time. Counting charCodeAt calls on
// receivers at least `minLen` long counts exactly the code units it scanned:
// parseBlocks only ever touches the split-off lines, which are far shorter, and
// the receiver-length check is O(1) so the spy costs nothing per call.
type CharCodeAtFn = typeof String.prototype.charCodeAt
const realCharCodeAt: CharCodeAtFn = String.prototype.charCodeAt

function countScannedUnitsOf(minLen: number): () => number {
  let n = 0
  const patched = function (this: string, ...args: Parameters<CharCodeAtFn>): number {
    if (this.length >= minLen) n += 1
    return realCharCodeAt.apply(this, args)
  }
  String.prototype.charCodeAt = patched as unknown as CharCodeAtFn
  return () => n
}

describe('useBlockAssembler streaming throttle', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => {
    String.prototype.split = realSplit
    String.prototype.charCodeAt = realCharCodeAt
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

  it('returns a stable reference when the text has not changed', () => {
    const { result, rerender } = renderHook(
      ({ text }) => useBlockAssembler(text, true),
      { initialProps: { text: 'alpha' } },
    )
    const first = result.current
    rerender({ text: 'alpha' })
    rerender({ text: 'alpha' })
    expect(result.current).toBe(first)
  })

  it('extends the tail block per render inside a window, keeping settled block identity', () => {
    const opening = 'settled paragraph\n```js\nconst x = 1'
    const { result, rerender } = renderHook(
      ({ text }) => useBlockAssembler(text, true),
      { initialProps: { text: opening } },
    )
    const before = result.current
    expect(before).toHaveLength(2)

    // No timer advance: the growth must be visible in the SAME window.
    rerender({ text: opening + ' + 2' })
    const after = result.current
    expect(after).toHaveLength(2)
    // Settled block: same object, so memoized renderers skip it.
    expect(after[0]).toBe(before[0])
    // Tail block: text is current even though no structural parse ran.
    expect(after[1].content).toBe('const x = 1 + 2')
    expect(after[1].complete).toBe(false)
  })

  it('starts a synthetic markdown tail when the snapshot ended on a closed block', () => {
    const closed = '```js\nconst x = 1\n```'
    const { result, rerender } = renderHook(
      ({ text }) => useBlockAssembler(text, true),
      { initialProps: { text: closed } },
    )
    act(() => { vi.advanceTimersByTime(THROTTLE_MS) })
    expect(result.current).toHaveLength(1)

    rerender({ text: closed + '\nAnd then' })
    expect(result.current).toHaveLength(2)
    expect(result.current[1].type).toBe('markdown')
    expect(result.current[1].content).toContain('And then')
  })

  it('scans the text for newlines once per structural parse, not once per frame, however long it is', () => {
    // Hundreds of KB of prose ending on a CLOSED fence, so every frame inside
    // the window takes the branch that starts a synthetic markdown tail -- the
    // one that needs the line number of the appended text. Before the count
    // was cached on the snapshot this branch rescanned the whole text per frame.
    const big = `${'lorem ipsum dolor sit amet\n'.repeat(8000)}\`\`\`js\nconst x = 1\n\`\`\``
    expect(big.length).toBeGreaterThan(200_000)
    const scanned = countScannedUnitsOf(big.length)

    const { result, rerender } = renderHook(
      ({ text }) => useBlockAssembler(text, true),
      { initialProps: { text: big } },
    )
    // The mount's structural parse pays exactly one scan of the text.
    expect(scanned()).toBe(big.length)

    const frames = 30
    for (let f = 1; f <= frames; f++) rerender({ text: `${big}\nframe ${f}` })
    // Thirty frames of tail extension: not one code unit of the text rescanned.
    expect(scanned()).toBe(big.length)
    // And the cached value is the same number the scan would have produced.
    const tail = result.current[result.current.length - 1]
    expect(tail.type).toBe('markdown')
    expect(tail.startLine).toBe(big.split('\n').length)

    // The next structural parse scans the (now longer) text exactly once more.
    const latest = `${big}\nframe ${frames}`
    act(() => { vi.advanceTimersByTime(THROTTLE_MS) })
    expect(scanned()).toBe(big.length + latest.length)
  })

  it('parses at throttle rate, not frame rate, across a simulated 60fps stream', () => {
    const frameMs = 16
    const frames = 60
    const parses = countParsesOf(MARK)

    const { rerender } = renderHook(
      ({ text }) => useBlockAssembler(text, true),
      { initialProps: { text: `${MARK} ` } },
    )
    for (let f = 1; f <= frames; f++) {
      rerender({ text: `${MARK} ${'word '.repeat(f)}` })
      act(() => { vi.advanceTimersByTime(frameMs) })
    }
    // One parse for the mount, then at most one per elapsed throttle window.
    const ceiling = 1 + Math.floor((frames * frameMs) / THROTTLE_MS)
    expect(parses()).toBeLessThanOrEqual(ceiling)
    // The stream did parse structurally along the way, so the bound is real.
    expect(parses()).toBeGreaterThan(1)
    expect(parses()).toBeLessThan(frames)
  })

  it('keeps every settled block by identity across many frames, in both tail branches', () => {
    // Open tail: the trailing fence is still provisional.
    const open = 'one\n```js\na\n```\ntwo\n```py\nb'
    const a = renderHook(
      ({ text }) => useBlockAssembler(text, true),
      { initialProps: { text: open } },
    )
    const openSettled = a.result.current.slice(0, -1)
    expect(openSettled).toHaveLength(3)
    for (let f = 1; f <= 10; f++) {
      a.rerender({ text: `${open}${f}` })
      const now = a.result.current
      expect(now).toHaveLength(4)
      openSettled.forEach((block, i) => expect(now[i]).toBe(block))
      expect(now[3].content).toBe(`b${f}`)
    }

    // Closed tail: the snapshot ends on a complete fence, so frames append a
    // synthetic markdown block after the settled ones.
    const closed = 'one\n```js\na\n```\ntwo\n```py\nb\n```'
    const b = renderHook(
      ({ text }) => useBlockAssembler(text, true),
      { initialProps: { text: closed } },
    )
    act(() => { vi.advanceTimersByTime(THROTTLE_MS) })
    const closedSettled = b.result.current
    expect(closedSettled).toHaveLength(4)
    for (let f = 1; f <= 10; f++) {
      b.rerender({ text: `${closed}\nthree ${f}` })
      const now = b.result.current
      expect(now).toHaveLength(5)
      closedSettled.forEach((block, i) => expect(now[i]).toBe(block))
      expect(now[4].type).toBe('markdown')
    }
  })

  it('does not extend across a rewrite that is not a pure extension', () => {
    const { result, rerender } = renderHook(
      ({ text }) => useBlockAssembler(text, true),
      { initialProps: { text: 'original text' } },
    )
    act(() => { vi.advanceTimersByTime(THROTTLE_MS) })
    const snap = result.current
    rerender({ text: 'rewritten' })
    // Not an extension: hold the snapshot; the next tick re-parses exactly.
    expect(result.current).toBe(snap)
    act(() => { vi.advanceTimersByTime(THROTTLE_MS) })
    expect(result.current).toEqual(parseBlocks('rewritten', true))
  })

  it('keeps structure stale inside a window but text fresh, and lands an exact parse when streaming ends', () => {
    const opening = 'intro\n```js\nconst x = 1'
    const closed = 'intro\n```js\nconst x = 1\n```\noutro'

    const { result, rerender } = renderHook(
      ({ text, streaming }) => useBlockAssembler(text, streaming),
      { initialProps: { text: opening, streaming: true } },
    )
    act(() => { vi.advanceTimersByTime(THROTTLE_MS) })
    expect(result.current).toEqual(parseBlocks(opening, true))

    rerender({ text: closed, streaming: true })
    // Pins that the throttle defers STRUCTURE: the closing fence has not been
    // reclassified yet (the code block is still provisional), but the streamed
    // characters are already visible inside the tail block.
    expect(result.current).toHaveLength(2)
    expect(result.current[1].complete).toBe(false)
    expect(result.current[1].content).toContain('outro')
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
