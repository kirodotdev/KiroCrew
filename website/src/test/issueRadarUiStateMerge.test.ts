import { beforeEach, describe, expect, it } from 'vitest'
import { loadUiState, patchUiState, saveUiState } from '../apps/issue-radar/lib/format'

// Issue Radar's provider persists view / filter / selection state on every change.
// It used to write the WHOLE document from one tab's React state, so two dashboard
// tabs clobbered each other: a tab persisting one unrelated edit rewrote every other
// field from whatever it read at mount, reverting the other tab's work. `aiLanguage`
// had a per-field carve-out; its siblings did not.
//
// The fix has two halves and neither works alone -- `saveUiState` merges over the
// on-disk document, AND its caller sends only the fields it changed. These pin the
// persistence half; the provider's diffing half is exercised through the context.
describe('issue-radar saveUiState merges instead of overwriting', () => {
  beforeEach(() => localStorage.clear())

  it('leaves fields it was not given alone', () => {
    saveUiState({ query: 'from-tab-a', sortKey: 'updated', requestedByMe: true })
    // A second tab persists one unrelated change.
    saveUiState({ mainView: 'settings' })

    const doc = loadUiState()
    expect(doc.mainView).toBe('settings')
    expect(doc.query).toBe('from-tab-a')
    expect(doc.sortKey).toBe('updated')
    expect(doc.requestedByMe).toBe(true)
  })

  it('still overwrites a field it IS given', () => {
    // The merge must not turn into an append-only store: a field the caller sends
    // is the caller's current value and has to win.
    saveUiState({ query: 'first' })
    saveUiState({ query: 'second' })
    expect(loadUiState().query).toBe('second')
  })

  it('does not revert a language set through its own writer', () => {
    // The case that used to need a carve-out: `setAiLanguage` patches the field
    // directly, and an unrelated save from another tab must not undo it.
    patchUiState({ aiLanguage: 'zh-CN' })
    saveUiState({ sortKey: 'created' })
    expect(loadUiState().aiLanguage).toBe('zh-CN')
  })

  it('preserves a nested refresh block written by another tab', () => {
    // `refresh` is the one field with a validated domain, and `patchUiState` excludes
    // it by type -- so `saveUiState` is its only merge-capable writer and must not
    // drop it when persisting something else.
    saveUiState({ refresh: { pollInBackground: true, listPollMs: 60_000 } })
    saveUiState({ prQuery: 'unrelated' })

    const doc = loadUiState()
    expect(doc.prQuery).toBe('unrelated')
    expect(doc.refresh).toMatchObject({ pollInBackground: true, listPollMs: 60_000 })
  })

  it('merges refresh MEMBER-WISE so two tabs do not revert each other', () => {
    // The clobber one level down: `refresh` holds five independent settings, so
    // replacing the whole object on any change means the tab that toggled polling and
    // the tab that changed an interval each undo the other's member.
    saveUiState({ refresh: { pollInBackground: true } })   // tab B toggles polling
    saveUiState({ refresh: { listPollMs: 30_000 } })       // tab A changes an interval

    expect(loadUiState().refresh).toMatchObject({
      pollInBackground: true,   // tab B's setting survived tab A's write
      listPollMs: 30_000,
    })
  })

  it('still overwrites a refresh member it IS given', () => {
    // Member-wise merge must not become append-only either.
    saveUiState({ refresh: { listPollMs: 30_000 } })
    saveUiState({ refresh: { listPollMs: 120_000 } })
    expect(loadUiState().refresh).toMatchObject({ listPollMs: 120_000 })
  })

  it('writes into an empty store without inventing fields', () => {
    saveUiState({ query: 'only' })
    expect(loadUiState()).toEqual({ query: 'only' })
  })
})

// Persistence is best-effort, so a failed write is swallowed rather than thrown --
// but the helper has to SAY whether it landed. The provider keeps a baseline of the
// document it last persisted and diffs against it; if a swallowed failure counted as
// a write, the fields in it would be marked stored while the document still held the
// old ones, so the next change would diff them as unchanged and never resend them.
describe('issue-radar saveUiState reports whether the write landed', () => {
  beforeEach(() => localStorage.clear())

  it('returns true on a normal write', () => {
    expect(saveUiState({ query: 'ok' })).toBe(true)
    expect(patchUiState({ query: 'also-ok' })).toBe(true)
  })

  it('returns false and leaves the document alone when storage refuses', () => {
    saveUiState({ query: 'durable' })

    const setItem = Storage.prototype.setItem
    Storage.prototype.setItem = () => { throw new Error('QuotaExceededError') }
    try {
      expect(saveUiState({ query: 'refused' })).toBe(false)
    } finally {
      Storage.prototype.setItem = setItem
    }

    expect(loadUiState().query).toBe('durable')
  })
})
