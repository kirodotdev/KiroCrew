/**
 * A side draft must come BACK, not merely be known to have existed.
 *
 * The record stored slot plus timestamp and dropped the text, so the only copy of the prose
 * lived in the composer's React state — the state the close unmounts. The guard could prove a
 * draft had existed while having nothing to restore, which is a report of the loss rather
 * than a recovery from it.
 *
 * Read back by SLOT, never by composer id: an id is minted per mount, so the composer that
 * comes back after a close cannot ask for the key its predecessor wrote.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { useSlotDraftPersistence } from '../hooks/useSlotDraftPersistence'
import { beginSlotQuiesce } from '../utils/slotComposerRegistry'
import {
  __resetForTests as resetSideDrafts,
  loadSideDrafts,
  readSideDraftForSlot,
  writeSideDraft,
} from '../utils/sideComposerDrafts'

const SLOT = 'chat-side-restore'
const PROSE = 'the half-written question that must not vanish'

describe('a side draft is recoverable, not merely detectable', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
    resetSideDrafts()
    localStorage.clear()
  })

  afterEach(() => {
    vi.useRealTimers()
    resetSideDrafts()
    localStorage.clear()
  })

  it('stores the TEXT, so the record can answer with the prose', () => {
    expect(writeSideDraft('composer-a', SLOT, PROSE)).toBe(true)
    expect(readSideDraftForSlot(SLOT)).toBe(PROSE)
  })

  it('still reports PRESENCE, so the close guard is unaffected', () => {
    writeSideDraft('composer-a', SLOT, PROSE)
    expect(loadSideDrafts()[SLOT] ?? []).toHaveLength(1)
  })

  it('answers for the SLOT even though the asking composer has a new id', () => {
    writeSideDraft('composer-gone-with-the-pane', SLOT, PROSE)
    // The successor knows its slot and nothing about its predecessor's key.
    expect(readSideDraftForSlot(SLOT)).toBe(PROSE)
  })

  it('returns the NEWEST when two panes each left one', () => {
    writeSideDraft('composer-older', SLOT, 'the older one')
    vi.advanceTimersByTime(5_000)
    writeSideDraft('composer-newer', SLOT, PROSE)
    expect(readSideDraftForSlot(SLOT)).toBe(PROSE)
  })

  it('does not answer for a DIFFERENT slot', () => {
    writeSideDraft('composer-a', 'chat-some-other-slot', PROSE)
    expect(readSideDraftForSlot(SLOT)).toBeNull()
  })

  it('hands a recovered draft back to a composer that mounts EMPTY', () => {
    writeSideDraft('composer-gone', SLOT, PROSE)
    const onRestore = vi.fn()
    renderHook(() => useSlotDraftPersistence(SLOT, '', onRestore))
    expect(onRestore).toHaveBeenCalledWith(PROSE)
  })

  it('does NOT overwrite text already on screen', () => {
    writeSideDraft('composer-gone', SLOT, PROSE)
    const onRestore = vi.fn()
    renderHook(() => useSlotDraftPersistence(SLOT, 'what the user is typing right now', onRestore))
    expect(onRestore).not.toHaveBeenCalled()
  })

  it('persists the late draft durably while a close is committing', () => {
    const release = beginSlotQuiesce(SLOT)
    const composer = renderHook(
      ({ text }: { text: string }) => useSlotDraftPersistence(SLOT, text),
      { initialProps: { text: '' } },
    )
    composer.rerender({ text: PROSE })
    release()
    // The whole point of the quiesce: the prose, not just the fact, outlives the close.
    expect(readSideDraftForSlot(SLOT)).toBe(PROSE)
  })
})

describe('a committed close does not claim it failed to close', () => {
  const HOOK = readFileSync(join(__dirname, '..', 'hooks', 'useSessionActions.ts'), 'utf-8')

  it('uses its own notice AFTER the delete has succeeded', () => {
    const commit = HOOK.indexOf('await dispatch(deleteSlot(slotKey)).unwrap()')
    expect(commit).toBeGreaterThan(-1)
    const after = HOOK.slice(commit)
    // The tab is already gone by here, so the pre-commit "Not closed" wording would assert
    // the one thing this branch knows to be false.
    expect(after).toContain("close_committed_late_draft")
    expect(after.slice(0, after.indexOf('close_committed_late_draft')))
      .not.toContain('close_vetoed_unsent')
  })

  it('keeps the veto wording only on paths that really refuse', () => {
    expect(HOOK.split('close_vetoed_unsent').length - 1).toBe(3)
    expect(HOOK.split('close_committed_late_draft').length - 1).toBe(1)
  })
})
