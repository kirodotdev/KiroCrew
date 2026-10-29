import { describe, it, expect } from 'vitest'
import { groupDisplayItems, applyRunningState } from '../pages/chat/groupDisplayItems'
import { findPinnedPromptIdx, findNextPromptIdx } from '../utils/pinnedPrompt'
import type { ChatMessage } from '../types'

// A turn the person steered twice, then took the second steer back. The taken-back
// row stays in the transcript as a text-less note, so it must never be the prompt
// the banner shows, nor the incoming prompt that pushes the banner out.
function steeredTurn(): ChatMessage[] {
  const out: ChatMessage[] = []
  const push = (role: string, content: string, meta?: Record<string, unknown>) =>
    out.push({ role, content, ts: '2026-10-23T05:00:00Z', meta } as unknown as ChatMessage)
  push('user', 'tag the release')
  push('assistant', 'Tagging')
  push('user', 'sign the tag', { steer: true, steerState: 'consumed', mid: 'm-1' })
  push('user', '', { steer: true, steerState: 'retracted', steerRetractAction: 'unsend', mid: 'm-2' })
  push('assistant', 'Signed and tagged.')
  return out
}

const contentAt = (items: ReturnType<typeof groupDisplayItems>, idx: number): string | null => {
  const item = items[idx]
  return item && item.kind === 'single' ? item.msg.content : null
}

describe('pinned prompt beside a taken-back steer', () => {
  it('pins the last steer the person kept, not the one they took back', () => {
    const items = applyRunningState(groupDisplayItems(steeredTurn()), false)

    const pinIdx = findPinnedPromptIdx(items, items.length - 1)

    expect(contentAt(items, pinIdx)).toBe('sign the tag')
  })

  it('does not treat the taken-back row as the next prompt', () => {
    const items = applyRunningState(groupDisplayItems(steeredTurn()), false)
    const kept = items.findIndex(it => it.kind === 'single' && it.msg.content === 'sign the tag')

    expect(findNextPromptIdx(items, kept)).toBe(-1)
  })
})
