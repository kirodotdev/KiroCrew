import { describe, it, expect } from 'vitest'
import { groupDisplayItems, applyRunningState, TURN_OPENER_ROLES } from '../pages/chat/groupDisplayItems'
import { findPinnedPromptIdx, jumpAnchorIdx } from '../utils/pinnedPrompt'
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

describe('pinned prompt in a nudge-driven session', () => {
  it('pins the nudge that opened the turn being read, not a prompt many cycles above', () => {
    const items = applyRunningState(groupDisplayItems(nudgeSession(20)), false)

    // Read position: the last row of the transcript (inside the final cycle).
    const handoffIdx = items.length - 1
    const pinIdx = findPinnedPromptIdx(items, handoffIdx)
    expect(pinIdx).toBeGreaterThanOrEqual(0)

    // The pinned row must be the nearest turn opener above the read position.
    // Skipping every nudge makes the banner point at the human's last typed
    // message — dozens of turns and tens of thousands of pixels away — which is
    // what makes the jump read as "teleported to the top".
    const gap = handoffIdx - pinIdx
    expect(gap).toBeLessThan(8)
  })

  it('the pinned row is a turn opener', () => {
    const items = applyRunningState(groupDisplayItems(nudgeSession(20)), false)
    const pinIdx = findPinnedPromptIdx(items, items.length - 1)
    const item = items[pinIdx]
    expect(item.kind).toBe('single')
    if (item.kind === 'single') {
      expect(TURN_OPENER_ROLES.has(item.msg.role)).toBe(true)
    }
  })

  // A workflow fan-out is the long-transcript twin of a babysit loop: subagent
  // completions open turns the same way nudges do, so excluding them would
  // reproduce the identical gap.
  it('pins a subagent completion that opened the turn being read', () => {
    const out: ChatMessage[] = []
    const push = (role: string, content: string, meta?: Record<string, unknown>) =>
      out.push({ role, content, ts: '2026-08-18T05:00:00Z', meta } as unknown as ChatMessage)
    push('user', 'the last thing the human actually typed')
    push('assistant', 'reply')
    for (let c = 1; c <= 12; c++) {
      push('subagent', `[Subagent completion event] agent-${c}\n\nfindings for ${c}`,
        { subagentCompletion: { kind: 'single', agentId: `agent-${c}`, outcome: 'ok', task: `task ${c}` } })
      push('assistant', `synthesis ${c}`)
    }
    expect(out.filter(m => isSubagentCompletionMessage(m)).length).toBe(12)
    const items = applyRunningState(groupDisplayItems(out), false)
    const handoffIdx = items.length - 1
    const pinIdx = findPinnedPromptIdx(items, handoffIdx)
    // Assert IDENTITY, not distance: with subagent excluded the transcript
    // collapses into few display items, so a gap threshold stays satisfied while
    // the banner silently points at the human's first message instead.
    const pinItem = items[pinIdx]
    expect(pinItem.kind).toBe('single')
    if (pinItem.kind === 'single') {
      expect(pinItem.msg.role).toBe('subagent')
      expect(pinItem.msg.content).toContain('agent-12')
    }
  })

  it('a run of back-to-back completions groups as adjacent openers and the jump anchors at its head', () => {
    // Four completions drained consecutively with no reply between them: the
    // subagent rows are ADJACENT display items — the run shape jumpAnchorIdx's
    // machine-opener walk exists for. Built through the real grouping pipeline
    // so the adjacency is a checked fact, not a hand-shaped fixture.
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

    // The four completions must be consecutive display items.
    const subIdxs = items
      .map((it, i) => (it.kind === 'single' && it.msg.role === 'subagent' ? i : -1))
      .filter(i => i >= 0)
    expect(subIdxs.length).toBe(4)
    expect(subIdxs[3] - subIdxs[0]).toBe(3)

    // Jumping to the third completion anchors at the first — the head of the
    // block, with the dispatch that explains it directly above.
    expect(jumpAnchorIdx(items, subIdxs[2])).toBe(subIdxs[0])
  })

  it('the pin scan and the grouping agree on the turn-opener roles', () => {
    // Single-sourced on purpose: a role that opens a turn without being pinnable
    // is exactly the drift that produced the 61-row gap. Each role needs a row
    // the grouping actually emits — a `subagent` row that is not a completion is
    // dropped before it becomes a display item, so it opens nothing.
    const validRow = (role: string): ChatMessage => role === 'subagent'
      ? ({ role, content: '[Subagent completion event] agent-1\n\nfindings', ts: 't',
           meta: { subagentCompletion: { kind: 'single', agentId: 'agent-1', outcome: 'ok', task: 'task' } } } as unknown as ChatMessage)
      : ({ role, content: 'opener', ts: 't' } as unknown as ChatMessage)

    for (const role of TURN_OPENER_ROLES) {
      const opener = validRow(role)
      if (role === 'subagent') expect(isSubagentCompletionMessage(opener)).toBe(true)
      const items = applyRunningState(groupDisplayItems([
        opener,
        { role: 'assistant', content: 'body', ts: 't' } as unknown as ChatMessage,
      ]), false)
      const pinIdx = findPinnedPromptIdx(items, items.length - 1)
      expect(pinIdx, `role ${role} opens a turn but is not pinnable`).toBeGreaterThanOrEqual(0)
    }
  })
})
