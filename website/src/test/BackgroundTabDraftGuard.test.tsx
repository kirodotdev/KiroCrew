/**
 * A background tab's draft must block a close too.
 *
 * The registry only knows MOUNTED composers, so a slot displayed by no live pane
 * answered "no unsent work" and the close destroyed a persisted draft in silence.
 * That made the confirm half-true: reliable on the active tab, absent everywhere
 * else, which teaches the user to trust a warning that does not always fire.
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest'

import { registerSlotComposer, nextComposerId, slotHasUnsentWork } from '../utils/slotComposerRegistry'
import { setDraft, saveDrafts, __resetForTests as resetDrafts } from '../utils/chatDrafts'

const SLOT = 'slot-background-draft'

describe('an unmounted tab still guards its draft', () => {
  let release: (() => void) | null = null

  beforeEach(() => {
    resetDrafts()
    localStorage.clear()
  })

  afterEach(() => {
    release?.()
    release = null
    resetDrafts()
    localStorage.clear()
  })

  it('reports unsent work for a slot with NO mounted pane', () => {
    // Positive control: the query must read clean first, or the assertion below
    // could pass on a store that was already dirty from another test.
    expect(slotHasUnsentWork(SLOT)).toBe(false)

    const drafts: Record<string, string> = {}
    setDraft(drafts, SLOT, 'half-written thought in a background tab')
    saveDrafts(drafts)

    expect(slotHasUnsentWork(SLOT)).toBe(true)
  })

  it('blocks on a POPOUT draft even while the local composer is empty', () => {
    // The registry is per-window; the draft store is shared localStorage. So a
    // popout's draft reaches storage but never this window's registry.

    // Short-circuiting on "a composer is mounted here" therefore stranded it, which
    // is the one case the guard exists for.
    const drafts: Record<string, string> = {}
    setDraft(drafts, SLOT, 'draft typed in a popout window')
    saveDrafts(drafts)

    release = registerSlotComposer(nextComposerId(), {
      getSlot: () => SLOT,
      hasWork: () => false,
    })

    expect(slotHasUnsentWork(SLOT)).toBe(true)
  })

  it('still sees a mounted draft when persistence is empty', () => {
    release = registerSlotComposer(nextComposerId(), {
      getSlot: () => SLOT,
      hasWork: () => true,
    })

    expect(slotHasUnsentWork(SLOT)).toBe(true)
  })
})
