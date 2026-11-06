import { describe, expect, it } from 'vitest'

import { kasRewindGuard, type KasRewindRun } from './kasRewindGuard'

const MESSAGE_TS = '2026-10-16T10:00:00Z'

const run = (overrides: Partial<KasRewindRun> = {}): KasRewindRun => ({
  slotKey: 'chat-1',
  createdAt: '2026-10-16T10:00:05Z',
  status: 'running',
  ...overrides,
})

describe('kasRewindGuard', () => {
  it('blocks the edit while a run started at or after the message is live, a paused one included', () => {
    expect(kasRewindGuard([run()], 'chat-1', MESSAGE_TS)).toBe('blocked')
    expect(kasRewindGuard([run({ createdAt: MESSAGE_TS })], 'chat-1', MESSAGE_TS)).toBe('blocked')
    expect(kasRewindGuard([run({ status: 'paused' })], 'chat-1', MESSAGE_TS)).toBe('blocked')
    // One live run blocks even beside an ended one.
    expect(kasRewindGuard([run({ status: 'completed' }), run()], 'chat-1', MESSAGE_TS)).toBe('blocked')
  })

  it('allows the edit with a note once every such run has ended', () => {
    for (const status of ['completed', 'failed', 'aborted']) {
      expect(kasRewindGuard([run({ status })], 'chat-1', MESSAGE_TS)).toBe('ended')
    }
  })

  it('ignores a run started before the message, and another chat\'s runs', () => {
    expect(kasRewindGuard([run({ createdAt: '2026-10-16T09:59:59Z' })], 'chat-1', MESSAGE_TS)).toBe('none')
    expect(kasRewindGuard([run({ slotKey: 'chat-2' })], 'chat-1', MESSAGE_TS)).toBe('none')
    expect(kasRewindGuard([], 'chat-1', MESSAGE_TS)).toBe('none')
  })

  it('fails closed: a run or message whose time cannot be read counts as started after the message', () => {
    expect(kasRewindGuard([run({ createdAt: null })], 'chat-1', MESSAGE_TS)).toBe('blocked')
    expect(kasRewindGuard([run({ createdAt: 'not a time' })], 'chat-1', MESSAGE_TS)).toBe('blocked')
    const before = run({ createdAt: '2026-10-16T09:00:00Z' })
    expect(kasRewindGuard([before], 'chat-1', null)).toBe('blocked')
    expect(kasRewindGuard([before], 'chat-1', 'not a time')).toBe('blocked')
    expect(kasRewindGuard([{ ...before, status: 'completed' }], 'chat-1', undefined)).toBe('ended')
  })

  it('answers none for a message with no chat', () => {
    expect(kasRewindGuard([run()], null, MESSAGE_TS)).toBe('none')
    expect(kasRewindGuard([run()], undefined, MESSAGE_TS)).toBe('none')
  })
})
