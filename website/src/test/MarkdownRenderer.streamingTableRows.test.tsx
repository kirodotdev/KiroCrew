import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, act } from '@testing-library/react'

/**
 * #37358: a streaming table is one markdown block, so every character typed
 * into its last row used to hand the whole table to react-markdown again. The
 * half-typed last row is now held back until its line break arrives, and a
 * block whose prepared text did not change is not parsed again.
 */

const parses = vi.hoisted(() => ({ count: 0 }))
vi.mock('react-markdown', async (importOriginal) => {
  const mod = await importOriginal<typeof import('react-markdown')>()
  const Real = mod.default
  function Counted(props: Parameters<typeof Real>[0]) {
    parses.count++
    return Real(props)
  }
  return { ...mod, default: Counted }
})

import MarkdownRenderer from '../components/MarkdownRenderer'
import { REVEAL_IDLE_SETTLE_MS } from '../components/markdown/streamingEffects'

const STREAM = { streaming: true, glow: true, smooth: false } as const
const TABLE = '| A | B |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |\n'

beforeEach(() => {
  parses.count = 0
})

describe('streaming table rows (#37358)', () => {
  it('does not parse the table again while the last row is being typed', () => {
    const lastRow = '| 5 | 6 |'
    const { rerender } = render(<MarkdownRenderer content={TABLE} {...STREAM} />)
    const afterFirst = parses.count
    for (let i = 1; i <= lastRow.length; i++) {
      rerender(<MarkdownRenderer content={TABLE + lastRow.slice(0, i)} {...STREAM} />)
    }
    expect(parses.count).toBe(afterFirst)
    // The line break completes the row: exactly one more parse, and it shows.
    rerender(<MarkdownRenderer content={TABLE + lastRow + '\n'} {...STREAM} />)
    expect(parses.count).toBe(afterFirst + 1)
  })

  it('renders each frame exactly as the text up to its last line break', () => {
    const full = TABLE + '| 5 | 6 |\n'
    for (let i = TABLE.length; i <= full.length; i++) {
      const frame = full.slice(0, i)
      const settled = frame.slice(0, frame.lastIndexOf('\n') + 1)
      const a = render(<MarkdownRenderer content={frame} {...STREAM} />)
      const b = render(<MarkdownRenderer content={settled} {...STREAM} />)
      expect(a.container.innerHTML).toBe(b.container.innerHTML)
      a.unmount()
      b.unmount()
    }
  })

  it('shows the last row once the stream ends, even without a line break', () => {
    const { container } = render(<MarkdownRenderer content={TABLE + '| 5 | 6 |'} />)
    expect(container.querySelectorAll('tbody tr')).toHaveLength(3)
  })

  it('keeps a prose line typed after a table visible', () => {
    const { container } = render(<MarkdownRenderer content={TABLE + '\nSome prose'} {...STREAM} />)
    expect(container.textContent).toContain('Some prose')
  })

  it('shows a held row once the stream pauses on it (a tool call mid-reply)', () => {
    vi.useFakeTimers()
    try {
      const { container } = render(<MarkdownRenderer content={TABLE + '| 5 | 6 |'} {...STREAM} />)
      expect(container.querySelectorAll('tbody tr')).toHaveLength(2)
      act(() => { vi.advanceTimersByTime(REVEAL_IDLE_SETTLE_MS) })
      expect(container.querySelectorAll('tbody tr')).toHaveLength(3)
    } finally {
      vi.useRealTimers()
    }
  })

  it('shows the last row when a fence opens right after the table', () => {
    const { container } = render(<MarkdownRenderer content={TABLE + '| 5 | 6 |\n```bash\necho hi'} {...STREAM} />)
    expect(container.querySelectorAll('tbody tr')).toHaveLength(3)
  })
})
