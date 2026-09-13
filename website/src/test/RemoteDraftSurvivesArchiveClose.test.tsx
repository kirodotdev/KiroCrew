/**
 * A draft typed in ANOTHER window during a single-slot archive must survive it.
 *
 * The closing window quiesces the slot for the whole round trip so a late keystroke is
 * written durably instead of debounced. That quiesce was in-memory, so the composer most
 * at risk — one mounted in a different window — never saw it: it kept debouncing, and the
 * archive's own removal broadcast unmounted it before the timer fired. The teardown then
 * cleared the key, so even a durable copy would not have outlived the unmount.
 *
 * Driven with the stamp written straight to storage, never through `beginSlotQuiesce`, so
 * the local Set stays empty and only the cross-window path can satisfy the assertion.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook } from '@testing-library/react'

import { useSlotDraftPersistence } from '../hooks/useSlotDraftPersistence'
import { beginSlotQuiesce, slotIsQuiescing } from '../utils/slotComposerRegistry'
import { __resetForTests as resetSideDrafts, loadSideDrafts } from '../utils/sideComposerDrafts'
import { DRAFT_SAVE_DEBOUNCE_MS } from '../utils/draftConstants'

const SLOT = 'chat-remote-archive'
const QUIESCE_KEY = `mc-slot-quiesce:${SLOT}`

function remoteComposer() {
  return renderHook(({ text }: { text: string }) => useSlotDraftPersistence(SLOT, text), {
    initialProps: { text: '' },
  })
}

describe('a remote composer draft survives an archive close it never initiated', () => {
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

  it('reads a quiesce PUBLISHED by another window, whose local Set is empty here', () => {
    localStorage.setItem(QUIESCE_KEY, String(Date.now()))
    expect(slotIsQuiescing(SLOT)).toBe(true)
  })

  it('writes a late remote keystroke durably before any debounce has elapsed', () => {
    localStorage.setItem(QUIESCE_KEY, String(Date.now()))
    const composer = remoteComposer()
    composer.rerender({ text: 'a question typed while the archive was in flight' })
    expect(loadSideDrafts()[SLOT] ?? []).toHaveLength(1)
  })

  it('KEEPS that draft when the removal broadcast unmounts the composer', () => {
    localStorage.setItem(QUIESCE_KEY, String(Date.now()))
    const composer = remoteComposer()
    composer.rerender({ text: 'a question typed while the archive was in flight' })
    composer.unmount()
    expect(loadSideDrafts()[SLOT] ?? []).toHaveLength(1)
  })

  it('KEEPS a draft written under a LOCAL quiesce when the composer unmounts', () => {
    const release = beginSlotQuiesce(SLOT)
    const composer = remoteComposer()
    composer.rerender({ text: 'late text under a local close' })
    composer.unmount()
    release()
    expect(loadSideDrafts()[SLOT] ?? []).toHaveLength(1)
  })

  it('still clears on unmount when NO close is in flight', () => {
    const composer = remoteComposer()
    composer.rerender({ text: 'an ordinary draft nobody is archiving' })
    vi.advanceTimersByTime(DRAFT_SAVE_DEBOUNCE_MS + 1)
    expect(loadSideDrafts()[SLOT] ?? []).toHaveLength(1)
    composer.unmount()
    expect(loadSideDrafts()[SLOT] ?? []).toHaveLength(0)
  })

  it('ignores a stamp older than the round trip it was meant to cover', () => {
    localStorage.setItem(QUIESCE_KEY, String(Date.now() - 120_000))
    expect(slotIsQuiescing(SLOT)).toBe(false)
  })
})
