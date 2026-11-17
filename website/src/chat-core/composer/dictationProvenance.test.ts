import { describe, it, expect, beforeEach, vi } from 'vitest'
import { queryClient } from '../../api/queryClient'
import {
  DICTATION_NOTE,
  PANE_DICTATION_SCOPE,
  _resetDictationProvenance,
  clearDictated,
  dictationNoteEnabled,
  isDictated,
  markDictated,
  noteComposerEdit,
  takeDictated,
  wantsDictationNote,
  withDictationNote,
} from './dictationProvenance'

/* The provenance record and the note, without any composer around them. The
 * composer and host tests drive these through real dictation and real sends;
 * these pin the rules those tests rely on. */

beforeEach(() => {
  _resetDictationProvenance()
  queryClient.removeQueries({ queryKey: ['sttConfig'] })
})

describe('dictation provenance record', () => {
  it('is kept per session, so marking one slot leaves every other slot typed', () => {
    markDictated('slot-a')
    expect(isDictated('slot-a')).toBe(true)
    expect(isDictated('slot-b')).toBe(false)
  })

  it('keys a composer with no session yet apart from every real session', () => {
    markDictated(null)
    expect(isDictated(null)).toBe(true)
    expect(isDictated(undefined)).toBe(true)
    expect(isDictated('slot-a')).toBe(false)
  })

  it('a send takes the record: it answers once and the next send from that slot is typed', () => {
    markDictated('slot-a')
    expect(takeDictated('slot-a')).toBe(true)
    expect(takeDictated('slot-a')).toBe(false)
  })

  it('an edit that empties the composer clears the record, any other edit keeps it', () => {
    markDictated('slot-a')
    noteComposerEdit('slot-a', 'fixed a misheard word')
    expect(isDictated('slot-a')).toBe(true)
    noteComposerEdit('slot-a', '  \n ')
    expect(isDictated('slot-a')).toBe(false)
  })

  it('keeps a pane\'s record apart from the main chat\'s for the same session', () => {
    markDictated('slot-a', PANE_DICTATION_SCOPE)
    expect(isDictated('slot-a', PANE_DICTATION_SCOPE)).toBe(true)
    expect(isDictated('slot-a')).toBe(false)
    expect(takeDictated('slot-a')).toBe(false)
    expect(takeDictated('slot-a', PANE_DICTATION_SCOPE)).toBe(true)
  })

  it('survives a reload of the tab: a fresh module reads the record back from session storage', async () => {
    markDictated('slot-a')
    markDictated('slot-b', PANE_DICTATION_SCOPE)
    clearDictated('slot-b', PANE_DICTATION_SCOPE)
    vi.resetModules()
    const fresh = await import('./dictationProvenance')
    expect(fresh.isDictated('slot-a')).toBe(true)
    expect(fresh.isDictated('slot-b', PANE_DICTATION_SCOPE)).toBe(false)
  })

  it('clearDictated forgets only the slot it names', () => {
    markDictated('slot-a')
    markDictated('slot-b')
    clearDictated('slot-a')
    expect(isDictated('slot-a')).toBe(false)
    expect(isDictated('slot-b')).toBe(true)
  })
})

describe('the setting', () => {
  it('reads off until the STT config says on, and follows a later change without a reload', () => {
    expect(dictationNoteEnabled()).toBe(false)
    expect(wantsDictationNote(true)).toBe(false)
    queryClient.setQueryData(['sttConfig'], { enabled: true, dictation_note: true })
    expect(dictationNoteEnabled()).toBe(true)
    expect(wantsDictationNote(true)).toBe(true)
    // Typed-only text never gets the note, whatever the setting.
    expect(wantsDictationNote(false)).toBe(false)
    queryClient.setQueryData(['sttConfig'], { enabled: true, dictation_note: false })
    expect(wantsDictationNote(true)).toBe(false)
  })

  it('never puts the note on a command, whose arguments it would become part of', () => {
    queryClient.setQueryData(['sttConfig'], { enabled: true, dictation_note: true })
    expect(wantsDictationNote(true, '/goal clear')).toBe(false)
    expect(wantsDictationNote(true, '  /model fast')).toBe(false)
    expect(wantsDictationNote(true, 'see /etc/hosts')).toBe(true)
  })
})

describe('the note', () => {
  it('is the exact sentence the feature promises', () => {
    expect(DICTATION_NOTE).toBe('[This message was dictated. It may contain transcription errors, odd typos, misplaced words, or punctuation.]')
  })

  it('goes on once, at the end, after a paragraph break', () => {
    expect(withDictationNote('hello there  \n')).toBe(`hello there\n\n${DICTATION_NOTE}`)
    expect(withDictationNote(withDictationNote('hello'))).toBe(`hello\n\n${DICTATION_NOTE}`)
  })

  it('is added after text that only mentions it mid-message', () => {
    expect(withDictationNote(`${DICTATION_NOTE} and then more`)).toBe(`${DICTATION_NOTE} and then more\n\n${DICTATION_NOTE}`)
  })
})
