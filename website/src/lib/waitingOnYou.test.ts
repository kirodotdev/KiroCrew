/** Status precedence for a pinned ask, and how its routes' refusals are read.
 *  A live approval or question card always outranks an ask, and an ask
 *  outranks a running turn, on every surface that draws a session's state. */
import { describe, expect, it } from 'vitest'
import { ApiError } from '../api/apiError'
import { actionForYouRowEntry, needsInputRowEntry } from '../pages/chat-sidebar/rowAskEntries'
import { actionForYouLabel } from '../components/waitingOnYou/ActionForYouStatus'
import type { WaitingOnYouAsk } from '../types'
import { TAB_STATUS_COLOR, tabStatus, tabStatusPulses } from './sessionTabs'
import { actionForYou, newestAsk, pinnedAsks, waitingRefusal } from './waitingOnYou'

const ask = (key: string, title: string): WaitingOnYouAsk => ({ key, ask_id: `wy-${key}`, title, detail: '', options: [], ts: 1, state: 'pinned' })
const two = [ask('a', 'Older ask'), ask('b', 'Newest ask')]

describe('pinned ask reading', () => {
  it('reads an absent or empty field as no asks', () => {
    expect(pinnedAsks(undefined)).toEqual([])
    expect(pinnedAsks({})).toEqual([])
    expect(newestAsk({ waiting_on_you: [] })).toBeNull()
  })

  it('takes the last ask in stack order as the newest', () => {
    expect(newestAsk({ waiting_on_you: two })?.title).toBe('Newest ask')
  })
})

describe('Action for you status precedence', () => {
  it('shows the newest ask when nothing more urgent is open', () => {
    expect(actionForYou({ waiting_on_you: two })?.key).toBe('b')
    expect(actionForYouLabel({ waiting_on_you: two })).toBe('Action for you · Newest ask')
  })

  it('a live approval or question card wins over a pinned ask', () => {
    expect(actionForYou({ waiting_on_you: two, pending_approval: true })).toBeNull()
    expect(actionForYou({ waiting_on_you: two, needs_input: true })).toBeNull()
    expect(actionForYouLabel({ waiting_on_you: two, needs_input: true })).toBe('')
  })

  it('the tab strip ranks an ask below approval and question, above running and unread', () => {
    expect(tabStatus({ waiting_on_you: two, pending_approval: true, running: true }, ['k'], 'k')).toBe('permission')
    expect(tabStatus({ waiting_on_you: two, needs_input: true, running: true }, ['k'], 'k')).toBe('question')
    expect(tabStatus({ waiting_on_you: two, running: true }, ['k'], 'k')).toBe('action')
    expect(tabStatus({ waiting_on_you: two }, ['k'], 'k')).toBe('action')
    expect(tabStatus({ waiting_on_you: [], running: true }, [], 'k')).toBe('running')
  })

  it('draws an ask in its own colour, separate from the question and approval states, and steady', () => {
    expect(TAB_STATUS_COLOR.action).toBe('var(--aim)')
    expect(new Set([TAB_STATUS_COLOR.question, TAB_STATUS_COLOR.permission, TAB_STATUS_COLOR.running]).has(TAB_STATUS_COLOR.action)).toBe(false)
    expect(tabStatusPulses('action')).toBe(false)
  })

  it('the sidebar row picks the question card first, then the ask', () => {
    const pick = (s: { waiting_on_you?: WaitingOnYouAsk[]; needs_input?: boolean; pending_approval?: boolean }) =>
      [needsInputRowEntry(!!s.needs_input, 'Needs your answer', '', 12), actionForYouRowEntry(s, '', 12)].find(e => e.when)?.key
    expect(pick({ waiting_on_you: two, needs_input: true })).toBe('needs_input')
    expect(pick({ waiting_on_you: two })).toBe('waiting_on_you')
    expect(pick({ waiting_on_you: two, pending_approval: true })).toBeUndefined()
    expect(pick({})).toBeUndefined()
  })
})

describe('waitingRefusal', () => {
  const err = (status: number, body: unknown) => new ApiError(status, 'x', JSON.stringify(body))

  it.each(['answered', 'superseded', 'cleared'] as const)('409 %s is a stale card', code => {
    expect(waitingRefusal(err(409, { ok: false, code }))).toEqual({ kind: 'stale', code })
  })

  it('reads 409 uncertain, 400 invalid_answer and a passed-through refusal', () => {
    expect(waitingRefusal(err(409, { ok: false, code: 'uncertain' }))).toEqual({ kind: 'uncertain' })
    expect(waitingRefusal(err(400, { ok: false, code: 'invalid_answer', error: 'no' }))).toEqual({ kind: 'invalid' })
    expect(waitingRefusal(err(409, { ok: false, code: 'crew_paused', error: 'Paused.' }))).toEqual({ kind: 'refused', code: 'crew_paused', reason: 'Paused.' })
  })

  it('treats a network error or a body with no code as retryable', () => {
    expect(waitingRefusal(new TypeError('Failed to fetch'))).toEqual({ kind: 'failed' })
    expect(waitingRefusal(new ApiError(502, 'Bad gateway', '<html>'))).toEqual({ kind: 'failed' })
  })
})
