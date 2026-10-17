import { describe, expect, it } from 'vitest'
import { answerRestoredCommandCenterNotice, answerRestoredNotice, questionName } from './questionAnswers'

// UX review on #15250: a restored-answer notice must name the question it hands
// back, and in the Command Center the session too, or several sessions' notices
// are indistinguishable from one another.
describe('questionName', () => {
  it('quotes the first question of the card on one line', () => {
    expect(questionName([{ question: ' Which\n  region? ' }, { question: 'Second' }])).toBe('Which region?')
  })

  it('shortens a long question with an ellipsis', () => {
    const long = 'x'.repeat(120)
    const name = questionName([{ question: long }])
    expect(name).toHaveLength(80)
    expect(name.endsWith('…')).toBe(true)
  })

  it('is empty when there is no question to name', () => {
    expect(questionName(undefined)).toBe('')
    expect(questionName([])).toBe('')
  })
})

describe('answerRestoredNotice', () => {
  it('names the question', () => {
    expect(answerRestoredNotice([{ question: 'Which region?' }]))
      .toBe('The agent stopped waiting for "Which region?", so your answers weren\'t sent. They\'re in the composer; send them if you still want to.')
  })

  it('falls back to the unnamed wording rather than rendering empty quotes', () => {
    const notice = answerRestoredNotice(undefined)
    expect(notice).toContain('stopped waiting for this question')
    expect(notice).not.toContain('""')
  })
})

describe('answerRestoredCommandCenterNotice', () => {
  it('names the session and the question', () => {
    expect(answerRestoredCommandCenterNotice('Which region?', 'Review worker'))
      .toBe('The agent in "Review worker" stopped waiting for "Which region?", so your answers weren\'t sent. They\'re in that session\'s composer; send them if you still want to.')
  })

  it('names only the question when the session title is unknown', () => {
    expect(answerRestoredCommandCenterNotice('Which region?', undefined))
      .toBe('The agent stopped waiting for "Which region?", so your answers weren\'t sent. They\'re in that session\'s composer; send them if you still want to.')
    expect(answerRestoredCommandCenterNotice('Which region?', '  ')).not.toContain('The agent in')
  })

  it('falls back to the unnamed wording rather than rendering empty quotes', () => {
    const notice = answerRestoredCommandCenterNotice('', 'Review worker')
    expect(notice).toContain('stopped waiting for this question')
    expect(notice).not.toContain('""')
  })
})
