/**
 * Pure helpers behind the session summary panel.
 *
 * These carry decisions rather than formatting: which open items get hoisted
 * into the triage block and in what order, and how a non-contiguous intent's
 * turn ranges are read back. Both are testable without rendering, so they are
 * tested here rather than through the DOM.
 */
import { describe, it, expect } from 'vitest'

import {
  collectTriage,
  formatRanges,
  resumptionCount,
  type SessionIntent,
} from '../types/sessionSummary'

function intent(over: Partial<SessionIntent> = {}): SessionIntent {
  return {
    title: 'a goal',
    initial_intent: '',
    progress: [],
    next_steps: [],
    ranges: [[1, 1]],
    status: 'active',
    verified: null,
    state: 'in-progress',
    last_touched_turn: 1,
    origin_turn: null,
    ...over,
  }
}

const step = (what: string) => ({ what, why: '', expect: '' })

describe('formatRanges', () => {
  it('renders a span as a range', () => {
    expect(formatRanges([[1, 14]])).toBe('1–14')
  })

  it('renders a single-turn intent as one number, not a degenerate range', () => {
    expect(formatRanges([[7, 7]])).toBe('7')
  })

  it('joins the several ranges a resumed intent carries', () => {
    expect(formatRanges([[1, 14], [77, 100]])).toBe('1–14, 77–100')
  })

  it('is empty for an intent with no ranges rather than throwing', () => {
    expect(formatRanges([])).toBe('')
  })
})

describe('resumptionCount', () => {
  it('is zero for a contiguous intent', () => {
    expect(resumptionCount(intent({ ranges: [[1, 9]] }))).toBe(0)
  })

  it('counts the gaps, not the ranges', () => {
    // Worked, dropped, picked back up: one resumption, two ranges.
    expect(resumptionCount(intent({ ranges: [[1, 14], [77, 100]] }))).toBe(1)
    expect(resumptionCount(intent({ ranges: [[1, 2], [5, 6], [9, 10]] }))).toBe(2)
  })

  it('does not go negative on an intent with no ranges', () => {
    expect(resumptionCount(intent({ ranges: [] }))).toBe(0)
  })
})

describe('collectTriage', () => {
  it('puts needs-you intents before merely recent ones', () => {
    // The ordering decision: "completed but never verified" is what a reader
    // forgets, so it outranks an open step on live work.
    const items = collectTriage([
      intent({ title: 'live work', state: 'in-progress', next_steps: [step('keep going')] }),
      intent({ title: 'unverified', state: 'needs-you', next_steps: [step('go look at it')] }),
    ])
    expect(items.map(i => i.what)).toEqual(['go look at it', 'keep going'])
  })

  it('carries the source intent so hoisting does not sever context', () => {
    const items = collectTriage([
      intent({ title: 'Rebrand the app', state: 'needs-you', next_steps: [step('fix the icon')] }),
    ])
    expect(items[0].fromIntent).toBe('Rebrand the app')
  })

  it('never hoists from a dropped intent', () => {
    // Abandoned work has no claim on the reader's attention.
    const items = collectTriage([
      intent({ title: 'abandoned', state: 'dropped', next_steps: [step('do not surface me')] }),
    ])
    expect(items).toEqual([])
  })

  it('includes steps from done intents only via the recent pass, not the needs-you pass', () => {
    const items = collectTriage([
      intent({ title: 'shipped', state: 'done', next_steps: [step('optional polish')] }),
    ])
    expect(items.map(i => i.what)).toEqual(['optional polish'])
  })

  it('respects the limit so the block cannot grow unbounded', () => {
    const many = intent({
      state: 'needs-you',
      next_steps: [step('one'), step('two'), step('three'), step('four')],
    })
    expect(collectTriage([many], 2)).toHaveLength(2)
  })

  it('is empty when nothing has an open step', () => {
    expect(collectTriage([intent(), intent()])).toEqual([])
  })

  it('preserves why and expect, which are what make a step decidable', () => {
    const items = collectTriage([
      intent({
        state: 'needs-you',
        next_steps: [{ what: 'run it', why: 'never verified', expect: 'confirms the fix' }],
      }),
    ])
    expect(items[0].why).toBe('never verified')
    expect(items[0].expect).toBe('confirms the fix')
  })

  it('does not list the same step twice when an intent is both needs-you and recent', () => {
    // The two passes must not double-count: the needs-you pass claims the item
    // and the recent pass skips that intent entirely.
    const items = collectTriage([
      intent({ title: 'once only', state: 'needs-you', next_steps: [step('single')] }),
    ])
    expect(items.filter(i => i.what === 'single')).toHaveLength(1)
  })
})
