import { describe, it, expect } from 'vitest'
import { countCompletedTurns, type TurnCountMessage } from './completedTurns'

const user = (): TurnCountMessage => ({ role: 'user' })
const asst = (): TurnCountMessage => ({ role: 'assistant' })
const notice = (kind: 'compaction' | 'session_reload'): TurnCountMessage => ({ role: 'assistant', kind })

describe('countCompletedTurns', () => {
  it('collapses a multi-assistant-message turn into a single back-and-forth', () => {
    // One user message answered by a tool step + the reply + a follow-up card.
    // The old tally (count every assistant message) returned 3 here, which is
    // exactly why the survey fired a turn or two early.
    expect(countCompletedTurns([user(), asst(), asst(), asst()])).toBe(1)
  })

  it('counts exactly 10 at the survey threshold, even with extra assistant messages and a notice', () => {
    // MIN_LIVE_TURNS in SessionPulseSurveyCard is 10, so this transcript is the
    // real boundary: the card must NOT show until this returns 10.
    const msgs: TurnCountMessage[] = []
    for (let i = 0; i < 10; i++) {
      msgs.push(user())
      msgs.push(asst()) // the reply
      if (i % 2 === 0) msgs.push(asst()) // some turns emit an extra assistant message
      if (i === 4) msgs.push(notice('compaction')) // a mid-stream status notice
    }
    // The old buggy rule (every assistant-role message) would count well over 10...
    expect(msgs.filter((m) => m.role === 'assistant').length).toBeGreaterThan(10)
    // ...but real completed back-and-forths is exactly 10.
    expect(countCompletedTurns(msgs)).toBe(10)
  })

  it('returns 9 (below the 10 threshold) for 9 completed exchanges', () => {
    const msgs: TurnCountMessage[] = []
    for (let i = 0; i < 9; i++) {
      msgs.push(user(), asst())
    }
    expect(countCompletedTurns(msgs)).toBe(9)
  })

  it('ignores system notices and an unanswered trailing user message', () => {
    const msgs = [
      notice('session_reload'), // status line before any real turn
      user(),
      asst(), // 1 real exchange
      user(), // sent, no reply yet
    ]
    expect(countCompletedTurns(msgs)).toBe(1)
  })

  it('does not count assistant messages with no preceding user message', () => {
    expect(countCompletedTurns([asst(), asst(), user(), asst()])).toBe(1)
  })

  it('reads the notice kind from meta.kind as well as the top-level kind', () => {
    const metaNotice: TurnCountMessage = { role: 'assistant', meta: { kind: 'compaction' } }
    expect(countCompletedTurns([user(), metaNotice, asst()])).toBe(1)
  })
})
