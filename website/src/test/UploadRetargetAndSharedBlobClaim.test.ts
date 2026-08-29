// Both cases GPT graded blocking at 6f19066eb9: an in-flight upload orphaned by a mode
// switch, and shared-blob drafts classified as recoverable across windows.
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it, beforeEach } from 'vitest'

import { clearSlotSuccession, recordSlotSuccession, resolveSlotSuccession } from '../utils/slotSuccession'
import { fileLandingSlot } from '../utils/uploadRouting'

const CHAT_PAGE = readFileSync(join(__dirname, '..', 'pages', 'ChatPage.tsx'), 'utf-8')

describe('an upload in flight across a mode switch', () => {
  beforeEach(() => clearSlotSuccession())

  it('lands in the replacement slot, not the deleted one', () => {
    recordSlotSuccession('slot-old', 'slot-new')
    expect(resolveSlotSuccession('slot-old')).toBe('slot-new')
  })

  it('routes the completion to the replacement draft bucket', () => {
    recordSlotSuccession('slot-old', 'slot-new')
    // The user has since moved on to a third slot, so the file goes to a draft, not the composer.
    expect(fileLandingSlot(resolveSlotSuccession('slot-old'), 'slot-other')).toEqual({
      target: 'draft',
      slot: 'slot-new',
    })
  })

  it('routes into the live composer when the replacement is the slot on screen', () => {
    recordSlotSuccession('slot-old', 'slot-new')
    expect(fileLandingSlot(resolveSlotSuccession('slot-old'), 'slot-new')).toEqual({ target: 'pending' })
  })

  it('follows a chain, because two switches in a row stale the first successor', () => {
    recordSlotSuccession('a', 'b')
    recordSlotSuccession('b', 'c')
    expect(resolveSlotSuccession('a')).toBe('c')
  })

  it('terminates on a cycle instead of spinning', () => {
    recordSlotSuccession('a', 'b')
    recordSlotSuccession('b', 'a')
    expect(['a', 'b']).toContain(resolveSlotSuccession('a'))
  })

  it('passes absence through so the router can still drop it', () => {
    expect(resolveSlotSuccession(null)).toBeNull()
    expect(resolveSlotSuccession(undefined)).toBeUndefined()
    expect(fileLandingSlot(resolveSlotSuccession(null), 'slot-a')).toEqual({ target: 'drop' })
  })

  it('leaves an unreplaced slot alone', () => {
    recordSlotSuccession('a', 'b')
    expect(resolveSlotSuccession('untouched')).toBe('untouched')
  })

  it('ignores a self-succession rather than recording a one-hop cycle', () => {
    recordSlotSuccession('a', 'a')
    expect(resolveSlotSuccession('a')).toBe('a')
  })

  it('bounds the table so a long-lived tab cannot accumulate slots', () => {
    for (let i = 0; i < 200; i++) recordSlotSuccession(`from-${i}`, `to-${i}`)
    expect(resolveSlotSuccession('from-199')).toBe('to-199')
    // The earliest entries were evicted, so the oldest key resolves to itself again.
    expect(resolveSlotSuccession('from-0')).toBe('from-0')
  })
})

describe('drafts persisted in a shared last-write-wins blob', () => {
  // The draft store documents `persistNow` as last-write-wins over the whole key, so no host
  // can honestly claim another window answers for its drafts while that holds.
  it('is hard-coded in the hook, so no host can declare otherwise', () => {
    const hook = readFileSync(join(__dirname, '..', 'hooks', 'useOptionActionDispatch.ts'), 'utf-8')
    expect(hook).toMatch(/useSlotComposerRegistration\(\s*\(\) => resolveSlotRef\.current\(\),\s*composerWorkRef\.current,\s*false,/)
    // The host-facing parameter is gone: its only caller passed the default.
    expect(CHAT_PAGE).not.toContain('workPersistedCrossWindow')
    expect(hook).not.toContain('workPersistedCrossWindow')
  })

})

// The module above is only half the fix: unwired, it retargets nothing. These pin the call
// sites, because the defect GPT found lives in the wiring rather than in the routing rule.
describe('the mode-switch handlers and upload completions are wired to it', () => {
  const count = (needle: string) => CHAT_PAGE.split(needle).length - 1

  it('positive control: the reader really is looking at ChatPage', () => {
    expect(CHAT_PAGE.length).toBeGreaterThan(10000)
    expect(count('copyDraftsToSlot(activeSlot, replacement)')).toBe(2)
  })

  it('records a succession at every slot replacement it copies drafts for', () => {
    expect(count('recordSlotSuccession(activeSlot, replacement)')).toBe(2)
  })

  it('resolves the captured slot at both upload completions', () => {
    expect(count('resolveSlotSuccession(requestSlot)')).toBe(2)
  })

  it('never writes a completion to the unresolved captured slot', () => {
    expect(CHAT_PAGE).not.toContain('fileLandingSlot(requestSlot,')
  })
})
