import { describe, it, expect } from 'vitest'
import { groupDisplayItems, applyRunningState } from '../pages/chat/groupDisplayItems'
import { findPinnedPromptIdx, jumpAnchorIdx } from '../utils/pinnedPrompt'
import type { ChatMessage } from '../types'

/**
 * A steer carries role `user`, so the pin's role test admits it — but it is
 * injected INTO a running turn rather than opening one, and its row lays out
 * between the opener and that turn's reply. `steered` builds that shape; the
 * flag is `meta.steer`, set by the `steer_push` echo.
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
  it('pins the row that OPENED the turn, not the steer injected into it', () => {
    const items = applyRunningState(groupDisplayItems(steered({ steer: true })), false)
    const openerIdx = rowIdx(items, 'add the resolution memo')
    const steerIdx = rowIdx(items, 'local gateway')
    expect(openerIdx).toBeGreaterThanOrEqual(0)
    expect(steerIdx).toBeGreaterThan(openerIdx)

    // Read position: the reply below the steer, so the steer has passed the line.
    const pinIdx = findPinnedPromptIdx(items, items.length - 1)
    expect(pinIdx).toBe(openerIdx)
    expect(pinIdx).not.toBe(steerIdx)
  })

  it('pins a plain second user row in the same shape, so meta.steer is what discriminates', () => {
    const items = applyRunningState(groupDisplayItems(steered({ steer: false })), false)
    const secondIdx = rowIdx(items, 'local gateway')
    expect(findPinnedPromptIdx(items, items.length - 1)).toBe(secondIdx)
  })

  it('a steer is never a jump target, so the jump lands on the opener', () => {
    const items = applyRunningState(groupDisplayItems(steered({ steer: true })), false)
    const openerIdx = rowIdx(items, 'add the resolution memo')
    expect(jumpAnchorIdx(items, openerIdx)).toBe(openerIdx)
  })

  it('falls back to the opener when the steer is the nearest row above the fold', () => {
    const items = applyRunningState(groupDisplayItems(steered({ steer: true })), false)
    const steerIdx = rowIdx(items, 'local gateway')
    expect(findPinnedPromptIdx(items, steerIdx + 1)).toBe(rowIdx(items, 'add the resolution memo'))
  })
})

describe('pinned prompt with a row another session authored', () => {
  /** A peer member's `session_send` (or a worker's report) lands as a user-role
   *  row carrying the gateway's `meta.sent_by`. The transcript draws it as a
   *  "From <sender>" card; the band must not quote it as this user's prompt. */
  function peerThenReply(withSentBy: boolean): ChatMessage[] {
    const out: ChatMessage[] = []
    const push = (role: string, content: string, meta?: Record<string, unknown>) =>
      out.push({ role, content, ts: '2026-09-14T06:45:00Z', meta } as unknown as ChatMessage)
    push('user', 'please confirm the retry test is green')
    push('assistant', 'It is green.')
    push('user', '[sent by session member-kirocrew-conductor via session_send]\n\nPEER-HELLO, one-line confirmation please.',
      withSentBy ? { sent_by: { session_key: 'member-kirocrew-conductor', via: 'session_send', member_slug: 'kirocrew-conductor' } } : undefined)
    push('assistant', 'Got it: PEER-HELLO received.')
    return out
  }

  it('skips the peer row and pins the last prompt the user typed', () => {
    const items = applyRunningState(groupDisplayItems(peerThenReply(true)), false)
    const pinned = findPinnedPromptIdx(items, items.length)
    expect(pinned).toBe(rowIdx(items, 'please confirm the retry test'))
  })

  it('a bare user row with the same text but no record is still a prompt (control)', () => {
    const items = applyRunningState(groupDisplayItems(peerThenReply(false)), false)
    const pinned = findPinnedPromptIdx(items, items.length)
    expect(pinned).toBe(rowIdx(items, 'PEER-HELLO'))
  })
})
