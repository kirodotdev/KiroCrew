import { describe, it, expect } from 'vitest'
import type { ChatMessage } from '../types'
import { replyShowsFooter } from '../pages/chat/replyFooter'

/**
 * The end-of-turn footer under a reply (model, credits, elapsed, then Copy /
 * Fork / More) is decided by ONE rule for the single-chat page and every SDK
 * host. A reply the runner stamped `turn_stats` on keeps its footer whatever is
 * drawn below it; a reply without stats waits for its turn to be over.
 */
const row = (role: string, over: Partial<ChatMessage> = {}): ChatMessage =>
  ({ role, content: `zzq-${role}`, cls: '', ...over }) as ChatMessage
const reply = (over: Partial<ChatMessage> = {}) => row('assistant', over)
const stamped = (over: Partial<ChatMessage> = {}) =>
  reply({ meta: { turn_stats: { elapsed_ms: 1200, credits: 0.42, model: 'zzq-model' } }, ...over })
/** The rule for the row at `index` of a list the caller draws as it is. */
const shows = (messages: ChatMessage[], index: number, running: boolean) =>
  replyShowsFooter(messages[index], messages, index, running)

describe('replyShowsFooter -- a reply that carries turn stats', () => {
  it('keeps its footer when an auto-nudge cycle follows and runs', () => {
    const messages = [row('user'), stamped(), row('nudge'), row('streaming')]
    expect(shows(messages, 1, true)).toBe(true)
  })

  it('keeps its footer when an error card and the recovery reply follow', () => {
    // A turn cut short by a capacity / transient error: partial reply, the
    // retrying card, the runner's recovery inject, then the recovered answer.
    const messages = [
      row('user'), stamped(), row('error', { meta: { kind: 'transient_retry' } }),
      row('inject', { meta: { injectKind: 'recovery' } }), reply(),
    ]
    expect(shows(messages, 1, false)).toBe(true)
  })

  it('keeps its footer while a later, unrelated run is going', () => {
    expect(shows([row('user'), stamped()], 1, true)).toBe(true)
  })

  it('does not count a zero elapsed measurement as stats', () => {
    const zero = reply({ meta: { turn_stats: { elapsed_ms: 0 } } })
    expect(shows([row('user'), zero, reply()], 1, false)).toBe(false)
  })
})

describe('replyShowsFooter -- a reply without turn stats', () => {
  it('shows the footer once a user row follows', () => {
    expect(shows([reply(), row('user')], 0, true)).toBe(true)
  })

  it('shows the footer once an auto-nudge cycle opens the next turn', () => {
    expect(shows([reply(), row('nudge'), reply()], 0, true)).toBe(true)
  })

  it('shows the footer once a sub-agent completion opens the next turn', () => {
    expect(shows([reply(), row('subagent'), reply()], 0, true)).toBe(true)
  })

  it('shows the footer once a cron inject opens the next turn', () => {
    const cron = row('inject', { meta: { injectKind: 'cron' } })
    expect(shows([reply(), cron, reply()], 0, true)).toBe(true)
  })

  it('withholds it past a recovery inject, which continues the same turn', () => {
    const recovery = row('inject', { meta: { injectKind: 'recovery' } })
    expect(shows([reply(), recovery, reply()], 0, false)).toBe(false)
  })

  it('withholds it when another reply of the same turn follows', () => {
    expect(shows([reply(), row('tool'), reply()], 0, false)).toBe(false)
  })

  it('passes over a system notice to the row that ends the turn', () => {
    const notice = reply({ meta: { kind: 'compaction' } })
    expect(shows([reply(), notice, row('user')], 0, true)).toBe(true)
    expect(shows([reply(), notice], 0, false)).toBe(true)
  })

  it('passes over a hidden say-nothing row to the row that ends the turn', () => {
    expect(shows([reply(), reply({ content: '\u200b' })], 0, false)).toBe(true)
  })

  it('shows it on the last row only once the session is idle', () => {
    expect(shows([reply()], 0, true)).toBe(false)
    expect(shows([reply()], 0, false)).toBe(true)
  })
})

describe('replyShowsFooter -- rows that never draw it', () => {
  it('never draws it on a streaming row', () => {
    expect(shows([row('streaming'), row('user')], 0, false)).toBe(false)
  })
})

describe('replyShowsFooter -- the drawn row and the live list differ', () => {
  // The page draws a deferred snapshot of its rows and hands the walk the live
  // list: right after older history lands, the drawn row's index points at a
  // different row of the live list until the deferred render catches up.
  const live = [row('user'), reply({ content: 'zzq-older' }), reply(), row('streaming')]

  it("reads the drawn row's own stats, not the live row at its index", () => {
    expect(replyShowsFooter(stamped(), live, 1, true)).toBe(true)
  })

  it("reads the drawn row's own role, not the live row at its index", () => {
    expect(replyShowsFooter(row('streaming'), [row('user'), stamped()], 1, false)).toBe(false)
  })
})
