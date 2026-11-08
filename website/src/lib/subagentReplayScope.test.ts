import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  REPLAY_SLOTS_PER_FRAME,
  bindSubagentReplaySender,
  replaySlotFrames,
  subagentReplaySlots,
  viewedSubagentSlots,
  viewSubagentSlots,
} from './subagentReplayScope'

afterEach(() => bindSubagentReplaySender(null))

describe('subagent replay scope', () => {
  it('asks the socket once for a slot two views share, and forgets it after both release', () => {
    const send = vi.fn()
    bindSubagentReplaySender(send)
    const releaseA = viewSubagentSlots(['chat-1', 'chat-2'])
    const releaseB = viewSubagentSlots(['chat-2', 'chat-3'])
    expect(send.mock.calls).toEqual([[['chat-1', 'chat-2']], [['chat-3']]])
    releaseA()
    expect(viewedSubagentSlots().sort()).toEqual(['chat-2', 'chat-3'])
    releaseB()
    expect(viewedSubagentSlots()).toEqual([])
  })

  it('names the focused slot first, then panes and registered views, once each', () => {
    const release = viewSubagentSlots(['chat-9', 'chat-1'])
    expect(subagentReplaySlots('chat-1', ['chat-2', 'chat-1'])).toEqual(['chat-1', 'chat-2', 'chat-9'])
    expect(subagentReplaySlots(null, [])).toEqual(['chat-9', 'chat-1'])
    release()
  })

  it('sends nothing while no socket is bound', () => {
    const release = viewSubagentSlots(['chat-1'])
    const send = vi.fn()
    bindSubagentReplaySender(send)
    expect(send).not.toHaveBeenCalled()
    release()
  })

  it('splits more slots than one frame carries across several frames', () => {
    const slots = Array.from({ length: REPLAY_SLOTS_PER_FRAME * 2 + 3 }, (_, i) => `chat-${i}`)
    const frames = replaySlotFrames(slots)
    expect(frames.map(f => f.length)).toEqual([REPLAY_SLOTS_PER_FRAME, REPLAY_SLOTS_PER_FRAME, 3])
    expect(frames.flat()).toEqual(slots)
    const send = vi.fn()
    bindSubagentReplaySender(send)
    const release = viewSubagentSlots(slots)
    expect(send.mock.calls.map(c => c[0].length)).toEqual([REPLAY_SLOTS_PER_FRAME, REPLAY_SLOTS_PER_FRAME, 3])
    release()
  })
})
