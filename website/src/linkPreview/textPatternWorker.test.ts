import { describe, it, expect } from 'vitest'
import { scanTexts, type FromWorker, type ToWorker } from './textPatternScan'
import './textPatternWorker'

/**
 * The scan worker's message contract, run in this thread. `textPatterns.test.tsx`
 * talks to an in-thread fake, so this is the test that runs the worker module.
 */
describe('textPatternWorker', () => {
  it('answers a scan with its pattern starts and one done, and ignores other messages', () => {
    const posted: FromWorker[] = []
    const ctx = self as unknown as {
      postMessage: (m: FromWorker) => void
      onmessage: (e: { data: unknown }) => void
    }
    ctx.postMessage = (m) => { posted.push(m) }

    ctx.onmessage({ data: { type: 'stop' } })
    ctx.onmessage({ data: null })
    expect(posted).toEqual([])

    const scan: ToWorker = {
      type: 'scan',
      batch: 3,
      // `a*` matches the empty string, so the scan rejects it.
      patterns: [{ source: 'TK-\\d+', flags: '' }, { source: 'a*', flags: '' }],
      texts: ['see TK-1234'],
    }
    ctx.onmessage({ data: scan })
    const starts: number[] = []
    scanTexts(scan.patterns, scan.texts, (i) => starts.push(i))
    expect(posted).toEqual([
      ...starts.map((i) => ({ type: 'pattern-start', batch: 3, i })),
      { type: 'done', batch: 3, hits: [[{ start: 4, end: 11, pattern: 0 }]], rejected: [1] },
    ])
  })
})
