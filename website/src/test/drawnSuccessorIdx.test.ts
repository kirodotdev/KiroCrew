import { describe, expect, it } from 'vitest'
import { drawnSuccessorIdx } from '../pages/chat/page/transcriptPins'
import type { ChatMessage } from '../types'

const user = (mid: string, meta: Record<string, unknown> = {}): ChatMessage => ({
  role: 'user', content: 'check the logs', cls: '', meta: { mid, ...meta },
})

describe('drawnSuccessorIdx', () => {
  it('sends a jump on a superseded steer to the row that ran in its place', () => {
    const rows = [user('m-old', { steer: true, steerState: 'superseded', supersededBy: 'm-new' }), user('m-other'), user('m-new')]
    expect(drawnSuccessorIdx(rows, 0)).toBe(2)
  })

  it('keeps the target when the row is not superseded', () => {
    const rows = [user('m-old', { steer: true, steerState: 'requeued', supersededBy: 'm-new' }), user('m-new')]
    expect(drawnSuccessorIdx(rows, 0)).toBe(0)
  })

  it('keeps the target when the successor is not loaded', () => {
    const rows = [user('m-old', { steer: true, steerState: 'superseded', supersededBy: 'm-gone' })]
    expect(drawnSuccessorIdx(rows, 0)).toBe(0)
  })
})

describe('drawnSuccessorIdx in a live tab', () => {
  const rebuilt = (content = 'check the logs'): ChatMessage => ({ role: 'user', content, cls: '', meta: {} })

  it('lands on the queue_pop rebuild, which has no mid yet', () => {
    const rows = [
      user('m-old', { steer: true, steerState: 'superseded', supersededBy: 'm-new' }),
      rebuilt('something else'),
      rebuilt(),
    ]
    expect(drawnSuccessorIdx(rows, 0)).toBe(2)
  })

  it('never picks an earlier row or a row the server already identified', () => {
    const rows = [
      rebuilt(),
      user('m-old', { steer: true, steerState: 'superseded', supersededBy: 'm-new' }),
      user('m-unrelated'),
    ]
    expect(drawnSuccessorIdx(rows, 1)).toBe(1)
  })
})
