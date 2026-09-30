/**
 * The two thread helpers every row set reads: which bubble gets a footer, and
 * which bubble gets the "Reply in thread" action.
 *
 * Both are pure functions over one message plus the render context, which is why
 * they are pinned here rather than through a rendered surface: the decision they
 * make is the contract, and it now has cases a rendered bubble cannot show (a
 * streaming row has no bubble footer at all).
 */
import { describe, it, expect, vi } from 'vitest'
import type { ChatMessage } from '../types'
import type { LegacyThreadSummary, SessionThreadSummary, ThreadSummary } from '../api/threads'
import { replyInThreadFor, threadCloseCardOf, threadFooterFor, threadMidOf, type MessageRenderContext, type ThreadHooks } from './messageRenderers'

const msg = (role: string, over: Partial<ChatMessage> = {}): ChatMessage =>
  ({ role, content: '', cls: '', ...over }) as ChatMessage

const LIVE: SessionThreadSummary = {
  kind: 'session',
  thread_slot: 'chat-77-1758524400',
  title: 'The other eight',
  opened_by: 'user',
  opened_at: 't',
  closed_at: null,
  summary_mid: null,
}
const LEGACY: LegacyThreadSummary = { kind: 'legacy', count: 2, last_reply_ts: 't', participants: ['user'] }

function ctxWith(threads: ThreadHooks | undefined, m: ChatMessage): MessageRenderContext {
  return {
    index: 0,
    messages: [m],
    running: false,
    key: 'k',
    hideCardOwnedOAuth: false,
    autoDeniedIds: new Set<string>(),
    threads,
    wrapper: (c) => c,
    row: (c) => c,
  }
}

const hooksFor = (map: Record<string, ThreadSummary>, onOpen = vi.fn()): ThreadHooks & { onOpen: ReturnType<typeof vi.fn> } => ({
  summaryOf: (mid: string) => map[mid],
  onOpen,
  crewmateName: 'Radar',
})

describe('threadMidOf', () => {
  it('reads the durable id and nothing else', () => {
    expect(threadMidOf(msg('assistant', { meta: { mid: 'm-1' } }))).toBe('m-1')
    expect(threadMidOf(msg('assistant'))).toBeUndefined()
    expect(threadMidOf(msg('assistant', { meta: { mid: '' } }))).toBeUndefined()
    expect(threadMidOf(msg('assistant', { meta: { mid: 7 } } as Partial<ChatMessage>))).toBeUndefined()
  })
})

describe('threadFooterFor', () => {
  it('draws for a live thread even before it has a single message', () => {
    // An anchor is the whole reason to draw: a thread opened a second ago has
    // nothing in it, and that is exactly when the user needs the way back in.
    const m = msg('assistant', { meta: { mid: 'm-1' } })
    expect(threadFooterFor(m, ctxWith(hooksFor({ 'm-1': LIVE }), m), 'start')).not.toBeNull()
  })

  it('draws for a version 1 thread that has replies, and not for one with none', () => {
    const m = msg('assistant', { meta: { mid: 'm-1' } })
    expect(threadFooterFor(m, ctxWith(hooksFor({ 'm-1': LEGACY }), m), 'start')).not.toBeNull()
    const empty = { ...LEGACY, count: 0 }
    expect(threadFooterFor(m, ctxWith(hooksFor({ 'm-1': empty }), m), 'start')).toBeNull()
  })

  it('draws nothing without a thread, without a mid, or on a surface with no hooks', () => {
    const m = msg('assistant', { meta: { mid: 'm-1' } })
    expect(threadFooterFor(m, ctxWith(hooksFor({}), m), 'start')).toBeNull()
    const noMid = msg('assistant')
    expect(threadFooterFor(noMid, ctxWith(hooksFor({ 'm-1': LIVE }), noMid), 'start')).toBeNull()
    expect(threadFooterFor(m, ctxWith(undefined, m), 'start')).toBeNull()
  })

  it('asks to READ, so an ended thread opens instead of being replaced', () => {
    // The footer is drawn for a thread that EXISTS and names it, ended or not, so
    // pressing it can only mean "show me that one". The row action is the other
    // intent and passes no `read`.
    const hooks = hooksFor({ 'm-1': { ...LIVE, closed_at: 't', summary_mid: 'm-card' } })
    const m = msg('assistant', { meta: { mid: 'm-1' } })
    const el = threadFooterFor(m, ctxWith(hooks, m), 'start') as React.ReactElement<{ onOpen: () => void }>
    el.props.onOpen()
    expect(hooks.onOpen).toHaveBeenCalledWith('m-1', { read: true })
  })
})

describe('threadCloseCardOf', () => {
  it('reads the back-link the gateway recorded', () => {
    const m = msg('assistant', {
      meta: { thread_summary: { thread_slot: 'chat-77-1758524400', title: 'The other eight' } },
    })
    expect(threadCloseCardOf(m)).toEqual({ threadSlot: 'chat-77-1758524400', title: 'The other eight' })
  })

  it('a card with no title is still a card', () => {
    // The title is the thread's, and a thread can be opened without one.
    const m = msg('assistant', { meta: { thread_summary: { thread_slot: 'chat-77-1758524400' } } })
    expect(threadCloseCardOf(m)).toEqual({ threadSlot: 'chat-77-1758524400', title: '' })
  })

  it('claims no ordinary row, and no record without a slot to open', () => {
    // The slot is the whole reason the row is a card: without one there is
    // nothing to open, so it renders as the reply it looks like.
    expect(threadCloseCardOf(msg('assistant'))).toBeNull()
    expect(threadCloseCardOf(msg('assistant', { meta: { mid: 'm-1' } }))).toBeNull()
    expect(threadCloseCardOf(msg('assistant', { meta: { thread_summary: {} } }))).toBeNull()
    expect(threadCloseCardOf(msg('assistant', { meta: { thread_summary: { thread_slot: 7 } } }))).toBeNull()
    expect(threadCloseCardOf(msg('assistant', { meta: { thread_summary: 'closed' } }))).toBeNull()
  })
})

describe('replyInThreadFor', () => {
  it('opens on the row\'s own mid', () => {
    const hooks = hooksFor({})
    const m = msg('assistant', { meta: { mid: 'm-1' } })
    replyInThreadFor(m, ctxWith(hooks, m))!()
    expect(hooks.onOpen).toHaveBeenCalledWith('m-1')
  })

  it('a STREAMING row gets the action and carries no mid', () => {
    // The reply being written has no id yet: ids are minted when the row
    // persists, post-turn. So the action passes none and the backend anchors the
    // thread to the message that started the turn. Withholding the action until
    // the reply finished was the old behaviour, and it withheld it at exactly
    // the moment a long answer going the wrong way is worth branching off.
    const hooks = hooksFor({})
    const m = msg('streaming')
    const open = replyInThreadFor(m, ctxWith(hooks, m))
    expect(open).toBeTypeOf('function')
    open!()
    expect(hooks.onOpen).toHaveBeenCalledWith(undefined)
  })

  it('a persisted row with no mid gets nothing', () => {
    // A pre-id transcript row. Unlike a streaming row there is no turn to fall
    // back to, so there is no anchor the backend could resolve.
    const hooks = hooksFor({})
    const m = msg('assistant')
    expect(replyInThreadFor(m, ctxWith(hooks, m))).toBeUndefined()
  })

  it('offers nothing on a surface with no hooks', () => {
    const m = msg('assistant', { meta: { mid: 'm-1' } })
    expect(replyInThreadFor(m, ctxWith(undefined, m))).toBeUndefined()
  })
})
