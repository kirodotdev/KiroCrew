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
const SUCCESSION = readFileSync(join(__dirname, '..', 'utils', 'slotSuccession.ts'), 'utf-8')
const count = (src: string, needle: string) => src.split(needle).length - 1

describe('a reactivated slot is not redirected into its deleted successor', () => {
  beforeEach(() => { clearSlotSuccession() })

  it('resolves to itself once the revoke has run, not to the gone successor', () => {
    recordSlotSuccession('slot-A', 'slot-B')
    // Pre-revoke: this is the misroute — new work in A lands in the deleted B.
    expect(resolveSlotSuccession('slot-A')).toBe('slot-B')
    // What activation does. B is gone; A is on screen and owns its own uploads again.
    forgetSlotSuccession('slot-A')
    expect(resolveSlotSuccession('slot-A')).toBe('slot-A')
  })

  it('is WIRED to activation, so resume, fork and a re-used key all revoke', () => {
    // The choke point: an upload or dictation can only start from the active slot, so
    // revoking on activation closes every route that makes a key live again.
    expect(CHAT_PAGE).toContain('useEffect(() => { if (activeSlot) forgetSlotSuccession(activeSlot) }, [activeSlot])')
  })

  it('still retargets an in-flight completion across a switch it did not activate', () => {
    recordSlotSuccession('slot-A', 'slot-B')
    forgetSlotSuccession('slot-B')
    expect(resolveSlotSuccession('slot-A')).toBe('slot-B')
  })
})

describe('an in-flight dictation survives a mode switch', () => {
  // The retargeting moved with the code: chat-core P3-b (#9787) extracted ChatPage's
  // voice subsystem into the Composer's Voice atom, so `applyVoiceText` no longer
  // lives here. The atom hands the host the RECORDING's own slot and resolves no
  // succession itself, so the same guarantees are now pinned on the two seams it
  // calls — `isComposerFor` (live splice or not) and `deliverOffScreen` (which
  // draft bucket).
  it('retargets the streaming partials instead of dropping them', () => {
    // The predicate consults the succession, so a retired origin resolves to the
    // slot now on screen and the partials keep splicing into the live composer.
    // Bare, the replacement became active and every remaining partial was discarded.
    expect(CHAT_PAGE).toContain('const live = resolveSlotSuccession(target) ?? null')
    expect(count(CHAT_PAGE,
      'target === activeSlotRef.current && composerSlotRef.current === target')).toBe(0)
  })

  it('routes the batch final to the surviving slot, not the deleted one', () => {
    // The off-screen append reads and writes the SUCCESSOR's draft; the deleted
    // slot's own key names a bucket nothing will ever read.
    expect(CHAT_PAGE).toContain('const live = resolveSlotSuccession(target) ?? target')
    expect(count(CHAT_PAGE, "const next = append(drafts.current[target] ?? '')")).toBe(0)
  })

  it('never lets an UNRESOLVABLE chain retarget the focused session', () => {
    // A capped chain answers null. Falling through to the active slot splices one
    // session's transcript into another's live composer, so null must fail the
    // predicate (routing off-screen) and keep the origin as the draft bucket —
    // never resolve to whatever happens to be focused.
    expect(CHAT_PAGE).toContain('resolveSlotSuccession(target) ?? null')
    expect(CHAT_PAGE).toContain('resolveSlotSuccession(target) ?? target')
    expect(count(CHAT_PAGE, 'resolveSlotSuccession(target) ?? activeSlotRef.current')).toBe(0)
  })

  it('resolves a LONG chain instead of dropping the completion', () => {
    clearSlotSuccession()
    for (let i = 0; i < 20; i++) recordSlotSuccession(`hop-${i}`, `hop-${i + 1}`)
    // 20 mode switches inside one upload's completion window used to exceed the walk bound,
    // so the resolver refused and the attachment was uploaded, charged and unreachable.
    expect(resolveSlotSuccession('hop-0')).toBe('hop-20')
  })

  it('still terminates on a CYCLE rather than spinning', () => {
    clearSlotSuccession()
    recordSlotSuccession('a', 'b')
    recordSlotSuccession('b', 'c')
    recordSlotSuccession('c', 'a')
    // Raising the bound without the `seen` guard would turn a dropped completion into a hang.
    expect(['a', 'b', 'c']).toContain(resolveSlotSuccession('a'))
  })

  it('walks the whole LIVE table, since a fully-referenced one may exceed the cap', () => {
    expect(SUCCESSION).toContain('hop < successors.size')
    expect(SUCCESSION).not.toContain('MAX_CHAIN')
    expect(SUCCESSION).toContain('seen.has(next)')
  })

  it('is reachable, because the mode switch records the succession first', () => {
    expect(CHAT_PAGE).toContain('recordSlotSuccession(activeSlot, replacement)')
  })
})

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

  it('is called on the failure path of the mode toggle', () => {
    // Two sites: the surviving toggle's catch, plus the activation revoke that drops
    // a succession whose source slot is live again. The clean-mode toggle that
    // supplied a third was removed upstream. A third now would need its own reason.
    expect(count(CHAT_PAGE, 'forgetSlotSuccession(activeSlot)')).toBe(2)
    // Inside the catch, not beside it: a successful delete must keep the retarget.
    // The catch now also reports the failure, so the retraction is asserted with
    // its neighbour rather than by pinning the whole block's former text.
    expect(count(CHAT_PAGE, '} catch (err: unknown) {\n                      // The slot survives, so the replacement must stop standing in for it.\n                      forgetSlotSuccession(activeSlot)')).toBe(1)
  })
})
