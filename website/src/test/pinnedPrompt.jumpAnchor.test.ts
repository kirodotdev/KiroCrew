import { describe, it, expect } from 'vitest'
import { jumpAnchorIdx } from '../utils/pinnedPrompt'
import type { DisplayItem } from '../pages/chat/groupDisplayItems'

// Minimal display items: only kind and msg.role are read by the helper.
const user = (): DisplayItem => ({ kind: 'single', msg: { role: 'user', content: 'q' } } as unknown as DisplayItem)
const steer = (): DisplayItem => ({ kind: 'single', msg: { role: 'user', content: 's', meta: { steer: true } } } as unknown as DisplayItem)
const asst = (): DisplayItem => ({ kind: 'single', msg: { role: 'assistant', content: 'a' } } as unknown as DisplayItem)
const nudge = (): DisplayItem => ({ kind: 'single', msg: { role: 'nudge', content: 'n' } } as unknown as DisplayItem)
const sub = (): DisplayItem => ({ kind: 'single', msg: { role: 'subagent', content: 'done' } } as unknown as DisplayItem)
const turn = (): DisplayItem => ({ kind: 'turn', items: [] } as unknown as DisplayItem)

describe('jumpAnchorIdx', () => {
  it('returns the target itself when a non-prompt row precedes it', () => {
    const items = [user(), asst(), user()]
    expect(jumpAnchorIdx(items, 2)).toBe(2)
  })

  it('walks to the head of a consecutive prompt run (steer after its prompt)', () => {
    // question(1) + steer(2) back to back: jumping to the steer must anchor at
    // the question, otherwise the question straddles the hand-off line after
    // landing and the banner unmounts (dead jump chain).
    const items = [asst(), user(), steer(), asst()]
    expect(jumpAnchorIdx(items, 2)).toBe(1)
  })

  it('walks across multiple consecutive user rows', () => {
    const items = [turn(), user(), user(), steer()]
    expect(jumpAnchorIdx(items, 3)).toBe(1)
  })

  it('stops at index 0 when the run reaches the top of the list', () => {
    const items = [user(), steer()]
    expect(jumpAnchorIdx(items, 1)).toBe(0)
  })

  it('does not treat a turn group above the target as part of the run', () => {
    const items = [user(), turn(), user()]
    expect(jumpAnchorIdx(items, 2)).toBe(2)
  })

  // Machine turn openers (nudge, subagent — TURN_OPENER_ROLES) are prompts
  // too: the walk consumes runs of them, anchoring at the head of the block
  // so the row that explains the run is read first.

  it('walks a subagent fan-out run to its head (synthesis still pending)', () => {
    // 4-agent fan-out, no synthesis row yet: user(0), assistant(1),
    // subagent(2..5). Pinning the third completion must anchor at the FIRST
    // completion — the fan-out reads as one block opened by subagent[0], with
    // the assistant's dispatch directly above it taking the straddle.
    const items = [user(), asst(), sub(), sub(), sub(), sub()]
    expect(jumpAnchorIdx(items, 4)).toBe(2)
  })

  it('walks consecutive unanswered nudge cycles to the head of the run', () => {
    // Three nudged turns that persisted no reply (errored or cancelled cycles
    // — a normal cycle interposes its tool/assistant rows): user(0),
    // assistant(1), nudge(2..4), assistant(5). Pinning the middle nudge
    // anchors at the first nudge of the run; the assistant row above it is
    // NOT consumed (it is not a turn opener).
    const items = [user(), asst(), nudge(), nudge(), nudge(), asst()]
    expect(jumpAnchorIdx(items, 3)).toBe(2)
  })

  it('a turn group between machine openers breaks the run', () => {
    // The shape a healthy babysit loop produces: each nudge's cycle collapses
    // into a turn group, so consecutive CYCLES never form one run. The walk
    // must stop at the turn group and return the target unchanged — walking
    // across cycles would send the jump many turns up the transcript.
    const items = [user(), asst(), nudge(), turn(), nudge()]
    expect(jumpAnchorIdx(items, 4)).toBe(4)
  })

  it('consumes a mixed run of different machine opener types', () => {
    // nudge(2) then subagent(3,4) back to back: the walk does not stop at a
    // type boundary — any TURN_OPENER_ROLES row extends the run, so pinning
    // the last subagent anchors at the nudge that heads the block.
    const items = [user(), asst(), nudge(), sub(), sub(), asst()]
    expect(jumpAnchorIdx(items, 4)).toBe(2)
  })

  it('extends a machine run into the user prompt that heads it', () => {
    // A user prompt immediately followed by machine openers (dispatch with no
    // assistant text row): the run is contiguous through the role change, so
    // the anchor is the human prompt at its head.
    const items = [asst(), user(), sub(), sub(), asst()]
    expect(jumpAnchorIdx(items, 3)).toBe(1)
  })
})
