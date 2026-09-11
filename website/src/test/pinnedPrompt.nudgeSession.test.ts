import { describe, it, expect } from 'vitest'
import { groupDisplayItems, applyRunningState, TURN_OPENER_ROLES } from '../pages/chat/groupDisplayItems'
import { findPinnedPromptIdx, findNextPromptIdx, jumpAnchorIdx } from '../utils/pinnedPrompt'
import { isSubagentCompletionMessage } from '../pages/chat/subagentCompletion'
import type { ChatMessage } from '../types'

// Reproduces the transcript shape a babysit/monitor loop produces: a few typed
// turns, then many auto-nudge cycles, each opening its own turn.
function nudgeSession(cycles: number): ChatMessage[] {
  const out: ChatMessage[] = []
  const push = (role: string, content: string, meta?: Record<string, unknown>) =>
    out.push({ role, content, ts: '2026-08-18T05:00:00Z', meta } as unknown as ChatMessage)

  push('user', 'the last thing the human actually typed')
  push('assistant', 'reply')
  for (let c = 1; c <= cycles; c++) {
    push('nudge', `[auto-nudge cycle ${c}]\nBabysit PR #4378 …`, { nudge: { cycle: c, loop_id: 'l1' } })
    push('tool', `Health check ${c}`)
    push('assistant', `cycle ${c} report`)
  }
  return out
}

function subagentSession(completions: number): ChatMessage[] {
  const out: ChatMessage[] = []
  const push = (role: string, content: string, meta?: Record<string, unknown>) =>
    out.push({ role, content, ts: '2026-08-18T05:00:00Z', meta } as unknown as ChatMessage)
  push('user', 'the last thing the human actually typed')
  push('assistant', 'reply')
  for (let c = 1; c <= completions; c++) {
    push('subagent', `[Subagent completion event] agent-${c}\n\nfindings for ${c}`,
      { subagentCompletion: { kind: 'single', agentId: `agent-${c}`, outcome: 'ok', task: `task ${c}` } })
    push('assistant', `synthesis ${c}`)
  }
  return out
}

function roleAt(items: ReturnType<typeof groupDisplayItems>, idx: number): string | null {
  const item = items[idx]
  return item && item.kind === 'single' ? item.msg.role : null
}

describe('pinned prompt in a nudge-driven session', () => {
  // The banner answers "what did I ask that this is a reply to". A nudge is
  // machine-injected on every cycle, so pinning it re-pins the band every few
  // screens while the user scrolls a babysit session — the defect this pins
  // against. The walk must skip every nudge and land on the human's prompt,
  // however far above it is.
  it('never pins a nudge: the banner lands on the last prompt the user typed', () => {
    const items = applyRunningState(groupDisplayItems(nudgeSession(20)), false)
    const handoffIdx = items.length - 1
    const pinIdx = findPinnedPromptIdx(items, handoffIdx)
    expect(pinIdx).toBeGreaterThanOrEqual(0)
    expect(roleAt(items, pinIdx)).toBe('user')
    const pinItem = items[pinIdx]
    if (pinItem.kind === 'single') {
      expect(pinItem.msg.content).toBe('the last thing the human actually typed')
    }
  })

  it('no display index a nudge row occupies is ever the pinned one', () => {
    const items = applyRunningState(groupDisplayItems(nudgeSession(20)), false)
    // Sweep the hand-off line down the whole transcript: whichever row is at the
    // fold, the pinned candidate must never be a nudge.
    for (let handoff = 0; handoff < items.length; handoff++) {
      const pinIdx = findPinnedPromptIdx(items, handoff)
      if (pinIdx >= 0) expect(roleAt(items, pinIdx), `handoff ${handoff}`).not.toBe('nudge')
    }
  })

  it('a nudge does not push the banner out either (it is not the next prompt)', () => {
    // findNextPromptIdx drives the push geometry: if a nudge counted as the
    // incoming prompt, the user's banner would be shoved out of the band by the
    // first cycle and never seen again for the rest of the loop.
    const items = applyRunningState(groupDisplayItems(nudgeSession(3)), false)
    const userIdx = items.findIndex(it => it.kind === 'single' && it.msg.role === 'user')
    expect(userIdx).toBeGreaterThanOrEqual(0)
    expect(findNextPromptIdx(items, userIdx)).toBe(-1)
  })

  it('never pins a subagent completion that opened the turn being read', () => {
    const msgs = subagentSession(12)
    expect(msgs.filter(m => isSubagentCompletionMessage(m)).length).toBe(12)
    const items = applyRunningState(groupDisplayItems(msgs), false)
    const pinIdx = findPinnedPromptIdx(items, items.length - 1)
    expect(pinIdx).toBeGreaterThanOrEqual(0)
    expect(roleAt(items, pinIdx)).toBe('user')
  })

  it('excludes a completion persisted under role user in older scrollback, by shape', () => {
    // Before the `subagent` role existed the same event was appended as a
    // `user` row. The role test alone would admit it; the parser must not.
    const out: ChatMessage[] = []
    const push = (role: string, content: string, meta?: Record<string, unknown>) =>
      out.push({ role, content, ts: 't', meta } as unknown as ChatMessage)
    push('user', 'typed prompt')
    push('assistant', 'reply')
    push('user', '[Subagent completion event] agent-1\n\nfindings',
      { subagentCompletion: { kind: 'single', agentId: 'agent-1', outcome: 'ok', task: 'task' } })
    push('assistant', 'synthesis')
    const items = applyRunningState(groupDisplayItems(out), false)
    const pinIdx = findPinnedPromptIdx(items, items.length - 1)
    const pinItem = items[pinIdx]
    expect(pinItem.kind).toBe('single')
    if (pinItem.kind === 'single') expect(pinItem.msg.content).toBe('typed prompt')
  })

  it('a run of back-to-back completions is not a prompt run: the jump anchors at the target itself', () => {
    const out: ChatMessage[] = []
    const push = (role: string, content: string, meta?: Record<string, unknown>) =>
      out.push({ role, content, ts: '2026-08-18T05:00:00Z', meta } as unknown as ChatMessage)
    push('user', 'fan out over four agents')
    push('assistant', 'dispatching')
    for (let c = 1; c <= 4; c++) {
      push('subagent', `[Subagent completion event] agent-${c}\n\nfindings for ${c}`,
        { subagentCompletion: { kind: 'single', agentId: `agent-${c}`, outcome: 'ok', task: `task ${c}` } })
    }
    push('assistant', 'synthesis of all four')
    const items = applyRunningState(groupDisplayItems(out), false)
    const subIdxs = items
      .map((it, i) => (it.kind === 'single' && it.msg.role === 'subagent' ? i : -1))
      .filter(i => i >= 0)
    expect(subIdxs.length).toBe(4)
    // The machine rows above the target are a non-prompt gap, so the anchor walk
    // does not consume them.
    expect(jumpAnchorIdx(items, subIdxs[2])).toBe(subIdxs[2])
  })

  it('the pin scan is narrower than the turn-opener set: only user rows are pinnable', () => {
    // Grouping and pinning are allowed to disagree here on purpose (see the
    // TURN_OPENER_ROLES docblock): every opener role still opens a turn, but only
    // the human's row can take the band.
    const validRow = (role: string): ChatMessage => role === 'subagent'
      ? ({ role, content: '[Subagent completion event] agent-1\n\nfindings', ts: 't',
           meta: { subagentCompletion: { kind: 'single', agentId: 'agent-1', outcome: 'ok', task: 'task' } } } as unknown as ChatMessage)
      : ({ role, content: 'opener', ts: 't' } as unknown as ChatMessage)

    for (const role of TURN_OPENER_ROLES) {
      const items = applyRunningState(groupDisplayItems([
        validRow(role),
        { role: 'assistant', content: 'body', ts: 't' } as unknown as ChatMessage,
      ]), false)
      const pinIdx = findPinnedPromptIdx(items, items.length - 1)
      if (role === 'user') expect(pinIdx, 'a user prompt must be pinnable').toBeGreaterThanOrEqual(0)
      else expect(pinIdx, `machine opener ${role} must not be pinnable`).toBe(-1)
    }
  })
})
