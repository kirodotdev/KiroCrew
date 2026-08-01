/**
 * `splitPaneMessages` — the one-pass replacement for four render-body filters.
 *
 * ChatPane owns the composer `input` state, so it re-renders on every keystroke.
 * Deriving the transcript with `allMessages.filter(...)` in the render body gave
 * it a fresh array identity per character, which permanently defeated the memo()
 * on ChatMessageList and re-ran its O(N) turn grouping while the user typed.
 *
 * These tests pin the split's SEMANTICS (the refactor's real risk is a logic
 * slip, not a perf regression) and the identity contract callers rely on.
 */
import { describe, it, expect } from 'vitest'
import { splitPaneMessages } from '../components/QueueStack'
import type { ChatMessage } from '../types'

const msg = (role: string, content = ''): ChatMessage =>
  ({ role, content, cls: '' } as ChatMessage)

const DELIVERY = '[Subagent completion event] agent finished'
const BATCH_DELIVERY = '[Subagent batch completion event] 3 agents finished'

describe('splitPaneMessages', () => {
  it('routes non-queued messages to the transcript, preserving order', () => {
    const all = [msg('user', 'a'), msg('assistant', 'b'), msg('tool', 'c')]
    const { messages, queuedMessages, systemDeliveryCount } = splitPaneMessages(all)
    expect(messages.map(m => m.content)).toEqual(['a', 'b', 'c'])
    expect(queuedMessages).toEqual([])
    expect(systemDeliveryCount).toBe(0)
  })

  it('keeps interactive queued entries out of the transcript and in the stack', () => {
    const all = [msg('user', 'a'), msg('queued', 'please do this next'), msg('assistant', 'b')]
    const { messages, queuedMessages } = splitPaneMessages(all)
    expect(messages.map(m => m.content)).toEqual(['a', 'b'])
    expect(queuedMessages.map(m => m.content)).toEqual(['please do this next'])
  })

  it('excludes sub-agent deliveries from the interactive stack but counts them', () => {
    const all = [msg('queued', DELIVERY), msg('queued', 'a real user message')]
    const { queuedMessages, systemDeliveryCount } = splitPaneMessages(all)
    // Editing or cancelling a delivery would silently lose a finished agent's
    // result — it must never render as an interactive card.
    expect(queuedMessages.map(m => m.content)).toEqual(['a real user message'])
    expect(systemDeliveryCount).toBe(1)
  })

  it('counts batch deliveries too', () => {
    const { queuedMessages, systemDeliveryCount } = splitPaneMessages([
      msg('queued', DELIVERY),
      msg('queued', BATCH_DELIVERY),
    ])
    expect(queuedMessages).toEqual([])
    expect(systemDeliveryCount).toBe(2)
  })

  it('applies the two queue predicates independently, not as else-if', () => {
    // A delivery satisfies BOTH isNonInteractiveQueued and isSystemDelivery.
    // Collapsing them into a single if/else would drop the count — the exact
    // slip this one-pass rewrite could have introduced.
    const { queuedMessages, systemDeliveryCount } = splitPaneMessages([msg('queued', DELIVERY)])
    expect(queuedMessages).toEqual([])
    expect(systemDeliveryCount).toBe(1)
  })

  it('never counts a non-queued message as a delivery', () => {
    // Same text, assistant role: the role gate must run first.
    const { messages, queuedMessages, systemDeliveryCount } = splitPaneMessages([msg('assistant', DELIVERY)])
    expect(messages).toHaveLength(1)
    expect(queuedMessages).toEqual([])
    expect(systemDeliveryCount).toBe(0)
  })

  it('handles an empty list', () => {
    const { messages, queuedMessages, systemDeliveryCount } = splitPaneMessages([])
    expect(messages).toEqual([])
    expect(queuedMessages).toEqual([])
    expect(systemDeliveryCount).toBe(0)
  })

  it('returns the same values a chain of filters would (equivalence oracle)', () => {
    const all = [
      msg('user', 'u1'), msg('queued', DELIVERY), msg('assistant', 'a1'),
      msg('queued', 'q1'), msg('queued', BATCH_DELIVERY), msg('tool', 't1'),
      msg('queued', 'q2'), msg('permission', 'p1'),
    ]
    const got = splitPaneMessages(all)

    // The exact expressions this replaced, kept here as the oracle so a future
    // edit to the one-pass loop cannot silently drift from the old semantics.
    const expectedMessages = all.filter(m => m.role !== 'queued')
    const allQueued = all.filter(m => m.role === 'queued')
    const expectedQueued = allQueued.filter(m => !(
      m.content.startsWith('[Subagent completion event]') ||
      m.content.startsWith('[Subagent batch completion event]')
    ))
    const expectedCount = allQueued.filter(m => (
      m.content.startsWith('[Subagent completion event]') ||
      m.content.startsWith('[Subagent batch completion event]')
    )).length

    expect(got.messages).toEqual(expectedMessages)
    expect(got.queuedMessages).toEqual(expectedQueued)
    expect(got.systemDeliveryCount).toBe(expectedCount)
  })

  it('returns fresh arrays per call, so callers MUST memoize', () => {
    // Documents why ChatPane wraps this in useMemo keyed on allMessages: the
    // function is pure but not memoizing, so calling it in a render body is
    // exactly the bug that was fixed.
    const all = [msg('user', 'a')]
    expect(splitPaneMessages(all).messages).not.toBe(splitPaneMessages(all).messages)
  })

  it('does not mutate its input', () => {
    const all = [msg('user', 'a'), msg('queued', 'q')]
    const snapshot = [...all]
    splitPaneMessages(all)
    expect(all).toEqual(snapshot)
  })
})
