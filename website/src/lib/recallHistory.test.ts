import { describe, it, expect } from 'vitest'
import { buildRecallHistory, type RecallMessage } from './recallHistory'

const user = (content: string): RecallMessage => ({ role: 'user', content })
const assistant = (content: string): RecallMessage => ({ role: 'assistant', content })

describe('buildRecallHistory', () => {
  it('returns transcript prompts oldest to newest, ignoring non-user rows', () => {
    expect(buildRecallHistory([user('one'), assistant('reply'), user('two')])).toEqual(['one', 'two'])
  })

  it('prefers rawText over content when both are present', () => {
    expect(buildRecallHistory([{ role: 'user', content: 'rendered', rawText: 'typed' }])).toEqual(['typed'])
  })

  it('collapses consecutive duplicates but keeps a non-consecutive repeat', () => {
    expect(buildRecallHistory([user('a'), user('a'), user('b'), user('a')])).toEqual(['a', 'b', 'a'])
  })

  it('skips empty rows', () => {
    expect(buildRecallHistory([user(''), user('real')])).toEqual(['real'])
  })

  // The defect this module exists for: without the merge these return only the
  // transcript, so the prompt the user watched vanish is unreachable by ↑.
  it('appends a submitted prompt the transcript never took', () => {
    expect(buildRecallHistory([user('landed')], ['lost'])).toEqual(['landed', 'lost'])
  })

  it('offers the lost prompt on the FIRST up press, i.e. at the tail', () => {
    const out = buildRecallHistory([user('older'), user('landed')], ['lost'])
    expect(out[out.length - 1]).toBe('lost')
  })

  it('recovers the prompt when the transcript is empty', () => {
    expect(buildRecallHistory([], ['lost'])).toEqual(['lost'])
  })

  it('does not offer a landed prompt twice', () => {
    expect(buildRecallHistory([user('same')], ['same'])).toEqual(['same'])
  })

  it('does not duplicate a landed prompt that is no longer the tail', () => {
    expect(buildRecallHistory([user('first'), user('second')], ['first', 'lost'])).toEqual(['first', 'second', 'lost'])
  })

  it('keeps only one copy of a prompt submitted twice and never landed', () => {
    expect(buildRecallHistory([], ['lost', 'lost'])).toEqual(['lost'])
  })

  it('ignores an empty submitted prompt', () => {
    expect(buildRecallHistory([user('landed')], [''])).toEqual(['landed'])
  })

  it('behaves identically with no attempts and with an empty attempts list', () => {
    const msgs = [user('one'), user('two')]
    expect(buildRecallHistory(msgs)).toEqual(buildRecallHistory(msgs, []))
  })
})
