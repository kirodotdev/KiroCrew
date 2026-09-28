import { describe, it, expect } from 'vitest'
import { appendFollowUpOption, removeFollowUpOption, type OwnedSuffix } from '../lib/followUpToggle'

/**
 * Pure-transform tests for the follow-up chip ownership helper (#7616). These
 * pin the two behaviours the ChatPane/ChatPage component tests exercise through
 * the UI, at the unit level where the edge cases are cheap to state — most
 * importantly F1: a label that itself contains ", " must round-trip, because
 * the option list is "|"-separated so such labels are legal.
 */
describe('followUpToggle — appendFollowUpOption', () => {
  it('appends onto an empty draft and records ownership', () => {
    expect(appendFollowUpOption('', null, 'Alpha')).toEqual({ value: 'Alpha', owned: { base: '', options: ['Alpha'] } })
  })

  it('appends onto a user draft, recording it as the base', () => {
    expect(appendFollowUpOption('note', null, 'Alpha')).toEqual({ value: 'note, Alpha', owned: { base: 'note', options: ['Alpha'] } })
  })

  it('extends the owned suffix when it is still intact at the tail', () => {
    const owned: OwnedSuffix = { base: 'note', options: ['Alpha'] }
    expect(appendFollowUpOption('note, Alpha', owned, 'Beta')).toEqual({
      value: 'note, Alpha, Beta',
      owned: { base: 'note', options: ['Alpha', 'Beta'] },
    })
  })

  it('re-baselines to the live draft when the user edited the owned tail', () => {
    const owned: OwnedSuffix = { base: '', options: ['Alpha'] }
    expect(appendFollowUpOption('Alpha and more', owned, 'Beta')).toEqual({
      value: 'Alpha and more, Beta',
      owned: { base: 'Alpha and more', options: ['Beta'] },
    })
  })

  it('keeps a comma-bearing label as ONE option (#7616 F1)', () => {
    const owned = appendFollowUpOption('', null, 'bar').owned
    const r = appendFollowUpOption('bar', owned, 'foo, bar')
    expect(r.value).toBe('bar, foo, bar')
    expect(r.owned).toEqual({ base: '', options: ['bar', 'foo, bar'] })
  })
})

describe('followUpToggle — removeFollowUpOption', () => {
  it('removes the only owned option, restoring the base', () => {
    const owned: OwnedSuffix = { base: 'note', options: ['Alpha'] }
    expect(removeFollowUpOption('note, Alpha', owned, 'Alpha')).toEqual({ value: 'note', owned: { base: 'note', options: [] } })
  })

  it('empties ownership to null when base and options are both empty', () => {
    const owned: OwnedSuffix = { base: '', options: ['Alpha'] }
    expect(removeFollowUpOption('Alpha', owned, 'Alpha')).toEqual({ value: '', owned: null })
  })

  it('removes only the un-toggled option, keeping the rest in order', () => {
    const owned: OwnedSuffix = { base: 'note', options: ['Alpha', 'Beta'] }
    expect(removeFollowUpOption('note, Alpha, Beta', owned, 'Alpha')).toEqual({
      value: 'note, Beta',
      owned: { base: 'note', options: ['Beta'] },
    })
  })

  it('leaves the draft untouched when the user edited the owned tail', () => {
    const owned: OwnedSuffix = { base: 'note', options: ['Alpha'] }
    // The user rewrote the draft to text that still ENDS with ', Alpha' but is
    // no longer the chip's own append — a content endsWith() would eat it.
    expect(removeFollowUpOption('other, Alpha', owned, 'Alpha')).toEqual({ value: 'other, Alpha', owned })
  })

  it('removes a comma-bearing label as ONE unit, never mis-split (#7616 F1)', () => {
    const owned: OwnedSuffix = { base: '', options: ['bar', 'foo, bar'] }
    // Un-toggle "bar" (the first option). A split-on-", " removal would break
    // the "foo, bar" label; the array removes exactly one element by identity.
    expect(removeFollowUpOption('bar, foo, bar', owned, 'bar')).toEqual({
      value: 'foo, bar',
      owned: { base: '', options: ['foo, bar'] },
    })
  })

  it('returns the draft unchanged when nothing is owned', () => {
    expect(removeFollowUpOption('anything', null, 'Alpha')).toEqual({ value: 'anything', owned: null })
  })
})
