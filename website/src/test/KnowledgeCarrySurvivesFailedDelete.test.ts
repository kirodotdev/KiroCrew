// GPT's F1 at 7387c6b5b9: a rejected slot DELETE left the old slot alive with its knowledge
// selection already stripped, because the in-memory move ran unconditionally.
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { beforeEach, describe, expect, it } from 'vitest'

import {
  clearSlotSuccession,
  forgetSlotSuccession,
  recordSlotSuccession,
  resolveSlotSuccession,
} from '../utils/slotSuccession'

const KNOWLEDGE = readFileSync(join(__dirname, '..', 'pages', 'chat', 'useKnowledgeFetch.ts'), 'utf-8')
const CHAT_PAGE = readFileSync(join(__dirname, '..', 'pages', 'ChatPage.tsx'), 'utf-8')
const count = (src: string, needle: string) => src.split(needle).length - 1

describe('the knowledge carry survives a rejected slot deletion', () => {
  it('copies without removing the source', () => {
    // The delete is awaited AFTER this runs, and it can be rejected.
    expect(KNOWLEDGE).toContain('slotMapRef.current.set(to, carried)')
    expect(count(KNOWLEDGE, 'slotMapRef.current.delete(from)')).toBe(0)
  })

  it('exposes a separate drop for the post-delete path', () => {
    expect(KNOWLEDGE).toContain('const dropCarriedKnowledge = useCallback((slot: string): void => {')
    expect(KNOWLEDGE).toContain('slotMapRef.current.delete(slot)')
    expect(KNOWLEDGE).toContain('carryPendingKnowledge, dropCarriedKnowledge }')
  })

  it('drops it only from the helper that runs after a SUCCESSFUL delete', () => {
    const dropAt = CHAT_PAGE.indexOf('knowledgeFetchRef.current.dropCarriedKnowledge(slot)')
    const inDropSlotDrafts = CHAT_PAGE.indexOf('const dropSlotDrafts = useCallback')
    const afterDropSlotDrafts = CHAT_PAGE.indexOf('const saveDraftsDebounced', inDropSlotDrafts)
    expect(dropAt).toBeGreaterThan(inDropSlotDrafts)
    expect(dropAt).toBeLessThan(afterDropSlotDrafts)
  })

  it('never drops it from the pre-delete copy', () => {
    const copyAt = CHAT_PAGE.indexOf('const copyDraftsToSlot = useCallback')
    const copyEnd = CHAT_PAGE.indexOf('const dropSlotDrafts = useCallback')
    const copyBody = CHAT_PAGE.slice(copyAt, copyEnd)
    expect(copyBody).toContain('carryPendingKnowledge(from, to)')
    expect(copyBody).not.toContain('dropCarriedKnowledge')
  })
})

describe('a succession is revoked when the deletion it anticipated fails', () => {
  beforeEach(() => clearSlotSuccession())

  it('stops standing in for a slot that survived', () => {
    recordSlotSuccession('slot-old', 'slot-new')
    expect(resolveSlotSuccession('slot-old')).toBe('slot-new')
    forgetSlotSuccession('slot-old')
    // The old slot is alive again, so its own uploads must land on it.
    expect(resolveSlotSuccession('slot-old')).toBe('slot-old')
  })

  it('leaves an unrelated succession alone', () => {
    recordSlotSuccession('a', 'b')
    recordSlotSuccession('c', 'd')
    forgetSlotSuccession('a')
    expect(resolveSlotSuccession('c')).toBe('d')
  })

  it('ignores an empty slot key', () => {
    recordSlotSuccession('a', 'b')
    forgetSlotSuccession('')
    expect(resolveSlotSuccession('a')).toBe('b')
  })

  it('is called on the failure path of both mode toggles', () => {
    expect(count(CHAT_PAGE, 'forgetSlotSuccession(activeSlot)')).toBe(2)
    // Inside the catch, not beside it: a successful delete must keep the retarget.
    expect(count(CHAT_PAGE, '} catch {\n                      // The slot survives, so the replacement must stop standing in for it.\n                      forgetSlotSuccession(activeSlot)\n                    }')).toBe(2)
  })
})
