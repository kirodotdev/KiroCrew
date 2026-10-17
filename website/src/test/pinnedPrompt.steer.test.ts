import { describe, it, expect } from 'vitest'
import { groupDisplayItems, applyRunningState } from '../pages/chat/groupDisplayItems'
import { findPinnedPromptIdx, findNextPromptIdx, jumpAnchorIdx } from '../utils/pinnedPrompt'
import type { ChatMessage } from '../types'

/**
 * A steer carries role `user` because the user typed it, and the banner admits
 * it: inside the reply that followed, it is the most recent thing they asked.
 * `steered` builds the shape where the steer arrives AFTER some output (a reply
 * row lies between it and its opener); `steeredImmediately` builds the shape
 * where it arrives before the turn produced anything, so it sits directly on its
 * opener. The flag is `meta.steer`, set by the `steer_push` echo.
 */
function steered(opts: { steer: boolean }): ChatMessage[] {
  const out: ChatMessage[] = []
  const push = (role: string, content: string, meta?: Record<string, unknown>) =>
    out.push({ role, content, ts: '2026-09-08T15:00:00Z', meta } as unknown as ChatMessage)

  push('user', 'add the resolution memo too, and draft the PR description')
  push('tool', 'Find the existing TTL constant')
  push('assistant', 'partial work')
  push('user', 'how can i apply it in my local gateway to test ?',
    opts.steer ? { steer: true } : undefined)
  push('assistant', "Here's the sequence. The order matters in two places …")
  return out
}

function steeredImmediately(): ChatMessage[] {
  const out: ChatMessage[] = []
  const push = (role: string, content: string, meta?: Record<string, unknown>) =>
    out.push({ role, content, ts: '2026-09-08T15:00:00Z', meta } as unknown as ChatMessage)

  push('user', 'add the resolution memo too, and draft the PR description')
  push('user', 'how can i apply it in my local gateway to test ?', { steer: true })
  push('assistant', "Here's the sequence. The order matters in two places …")
  return out
}

/** Display index of the row holding `needle`, or -1. Turn-wrapped rows are
 *  searched too, so no assertion depends on where grouping put it. */
function rowIdx(items: ReturnType<typeof applyRunningState>, needle: string): number {
  return items.findIndex(it => {
    if (it.kind === 'single') return it.msg.content.includes(needle)
    if (it.kind === 'group') return it.msgs.some(m => m.content.includes(needle))
    return it.items.some(t => t.kind === 'single'
      ? t.msg.content.includes(needle)
      : t.msgs.some(m => m.content.includes(needle)))
  })
}

describe('pinned prompt with a steer inside the turn', () => {
  it('pins the steer, not the opener it interrupted', () => {
    const items = applyRunningState(groupDisplayItems(steered({ steer: true })), false)
    const openerIdx = rowIdx(items, 'add the resolution memo')
    const steerIdx = rowIdx(items, 'local gateway')
    expect(openerIdx).toBeGreaterThanOrEqual(0)
    expect(steerIdx).toBeGreaterThan(openerIdx)

    // Read position: the reply below the steer, so the steer has passed the line.
    // It is the last thing the reader asked, which is the question the banner
    // answers — the opener stays reachable one step up the jump chain.
    expect(findPinnedPromptIdx(items, items.length - 1)).toBe(steerIdx)
  })

  it('pins the same row with the flag removed, so eligibility does not read meta.steer', () => {
    // Negative control in the other direction: a plain second user row in the
    // identical shape pins identically. If the pin ever discriminates on the
    // flag again, these two indices diverge.
    const withFlag = applyRunningState(groupDisplayItems(steered({ steer: true })), false)
    const without = applyRunningState(groupDisplayItems(steered({ steer: false })), false)
    const pinWith = findPinnedPromptIdx(withFlag, withFlag.length - 1)
    const pinWithout = findPinnedPromptIdx(without, without.length - 1)
    expect(pinWith).toBe(rowIdx(withFlag, 'local gateway'))
    expect(pinWith).toBe(pinWithout)
  })

  it('takes the band as soon as it clears the hand-off line', () => {
    const items = applyRunningState(groupDisplayItems(steered({ steer: true })), false)
    const steerIdx = rowIdx(items, 'local gateway')
    expect(findPinnedPromptIdx(items, steerIdx + 1)).toBe(steerIdx)
  })

  it('pushes the opener out of the band, being the next prompt after it', () => {
    // findNextPromptIdx drives the push geometry: the steer is what shoves the
    // opener's banner up, which is the same relationship any two consecutive
    // prompts have.
    const items = applyRunningState(groupDisplayItems(steered({ steer: true })), false)
    const openerIdx = rowIdx(items, 'add the resolution memo')
    expect(findNextPromptIdx(items, openerIdx)).toBe(rowIdx(items, 'local gateway'))
  })

  it('anchors the jump on its opener when it sits directly on it', () => {
    // No output between the two rows: they are one consecutive prompt run, so
    // clicking the pinned steer scrolls to the prompt that started the work.
    // Landing on the steer itself would leave the opener straddling the
    // hand-off line — unpinnable while already pushing the fallback banner out.
    const items = applyRunningState(groupDisplayItems(steeredImmediately()), false)
    const openerIdx = rowIdx(items, 'add the resolution memo')
    const steerIdx = rowIdx(items, 'local gateway')
    expect(steerIdx).toBeGreaterThan(openerIdx)
    expect(jumpAnchorIdx(items, steerIdx)).toBe(openerIdx)
  })

  it('is its own anchor when a reply row lies between it and its opener', () => {
    const items = applyRunningState(groupDisplayItems(steered({ steer: true })), false)
    const steerIdx = rowIdx(items, 'local gateway')
    expect(jumpAnchorIdx(items, steerIdx)).toBe(steerIdx)
  })
})
