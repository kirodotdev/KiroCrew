import { join } from 'node:path'
import { readFileSync } from 'node:fs'
import { vetoLiveClosingIntent } from '../utils/slotClosingIntent'
/**
 * The closing handshake must let another window refuse a delete.
 *
 * The race: window A's final dirty check passes, window B starts a draft, A's DELETE lands,
 * B's composer unmounts and the only copy of that draft is gone. B's claim is published on
 * a <=300ms debounce, so it was not in storage when A looked. The fix asks first — A
 * publishes an intent, B flushes its claim and vetoes, and the veto ABORTS the delete.
 *
 * These exercise the mechanism directly rather than the source text: the pre-fix ordering
 * is "check then DELETE with nothing in between", so the discriminating assertion is that
 * a veto arriving AFTER the check still stops the delete.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  __resetClosingIntentForTests,
  awaitClosingAcks,
  clearClosingIntent,
  closingIntentVetoed,
  publishClosingIntent,
  readClosingIntent,
  vetoClosingIntent,
  CLOSING_ACK_WINDOW_MS,
  CLOSING_KEY_PREFIX,
} from '../utils/slotClosingIntent'
import {
  __answerClosingIntentForTests,
  nextComposerId,
  registerSlotComposer,
  slotUnsentWorkSource,
  beginSlotQuiesce,
  slotIsQuiescing,
} from '../utils/slotComposerRegistry'
import { __resetSlotDirtyForTests, SLOT_DIRTY_KEY_PREFIX } from '../utils/slotDirtyBeacon'

const SLOT = 'chat-1281-1785676802'

const keysUnder = (prefix: string): string[] => {
  const out: string[] = []
  for (let i = 0; i < localStorage.length; i += 1) {
    const k = localStorage.key(i)
    if (k && k.startsWith(prefix)) out.push(k)
  }
  return out
}

const intentEvent = (slot: string) =>
  new StorageEvent('storage', { key: `${CLOSING_KEY_PREFIX}intent:${slot}`, newValue: '1' })

describe('a live composer can veto a pending close', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetClosingIntentForTests()
    __resetSlotDirtyForTests()
  })
  afterEach(() => {
    vi.restoreAllMocks()
    localStorage.clear()
  })

  it('publishes an intent that another window can read', () => {
    const nonce = publishClosingIntent(SLOT)
    expect(nonce).toBeTruthy()
    expect(readClosingIntent(SLOT)?.n).toBe(nonce)
    // Positive control: the reader is not answering yes to everything.
    expect(readClosingIntent('chat-does-not-exist')).toBeNull()
  })

  it('does NOT read silence as consent-by-default, only as no veto', () => {
    const nonce = publishClosingIntent(SLOT)!
    expect(closingIntentVetoed(nonce)).toBe(false)
    vetoClosingIntent(nonce, 'composer-x-1')
    expect(closingIntentVetoed(nonce)).toBe(true)
  })

  it('THE RACE: a composer that becomes dirty after the check still vetoes in time', () => {
    // Window B mounts a composer that is CLEAN at first — this is the state window A's
    // final check sees, and on the pre-fix code the DELETE went out here.
    let bHasWork = false
    const bId = nextComposerId()
    const unregister = registerSlotComposer(bId, { getSlot: () => SLOT, hasWork: () => bHasWork })
    expect(slotUnsentWorkSource(SLOT)).toBeNull()

    // A announces the close.
    const nonce = publishClosingIntent(SLOT)!

    // B's user types INSIDE the window A used to delete in. B's claim is still debounced,
    // so the claim tiers alone cannot see it yet.
    bHasWork = true

    // B answers the intent: flushes its claim and vetoes.
    __answerClosingIntentForTests(intentEvent(SLOT))

    expect(closingIntentVetoed(nonce)).toBe(true)
    // And the flush means even a caller that only re-reads the tiers now sees the work.
    expect(slotUnsentWorkSource(SLOT)).not.toBeNull()
    unregister()
  })

  it('a CLEAN composer does not veto, so a close of an idle slot still proceeds', () => {
    const id = nextComposerId()
    const unregister = registerSlotComposer(id, { getSlot: () => SLOT, hasWork: () => false })
    const nonce = publishClosingIntent(SLOT)!
    __answerClosingIntentForTests(intentEvent(SLOT))
    expect(closingIntentVetoed(nonce)).toBe(false)
    unregister()
  })

  it('ignores an intent for a slot this window does not hold', () => {
    let hasWork = true
    const id = nextComposerId()
    const unregister = registerSlotComposer(id, { getSlot: () => 'chat-other', hasWork: () => hasWork })
    const nonce = publishClosingIntent(SLOT)!
    __answerClosingIntentForTests(intentEvent(SLOT))
    expect(closingIntentVetoed(nonce)).toBe(false)
    hasWork = false
    unregister()
  })

  it('fails CLOSED when the veto store cannot be enumerated', () => {
    const nonce = publishClosingIntent(SLOT)!
    vi.spyOn(Storage.prototype, 'key').mockImplementation(() => { throw new Error('denied') })
    // A veto we cannot see is exactly the case the handshake exists for.
    expect(closingIntentVetoed(nonce)).toBe(true)
  })

  it('reports no intent when storage refuses the write, so the caller can fall back', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('quota') })
    expect(publishClosingIntent(SLOT)).toBeNull()
  })

  it('clears the intent and every answer to it', () => {
    const nonce = publishClosingIntent(SLOT)!
    vetoClosingIntent(nonce, 'composer-x-1')
    clearClosingIntent(SLOT, nonce)
    expect(readClosingIntent(SLOT)).toBeNull()
    expect(closingIntentVetoed(nonce)).toBe(false)
    const left = keysUnder(CLOSING_KEY_PREFIX)
    expect(left).toEqual([])
  })

  it('keeps its keys out of the dirty beacon namespace', () => {
    // A key under `mc-slot-dirty:` would be classified as a claim or, double-prefixed, as
    // a write-failure record — either would hold every slot shut.
    const nonce = publishClosingIntent(SLOT)!
    vetoClosingIntent(nonce, 'composer-x-1')
    const ours = keysUnder(CLOSING_KEY_PREFIX)
    expect(ours.length).toBeGreaterThan(0)
    for (const key of ours) expect(key.startsWith(SLOT_DIRTY_KEY_PREFIX)).toBe(false)
  })

  it('bounds the wait so an unanswering window cannot wedge the close', async () => {
    const started = Date.now()
    await awaitClosingAcks(1)
    expect(Date.now() - started).toBeLessThan(CLOSING_ACK_WINDOW_MS + 500)
    // The shipped default must comfortably exceed the draft debounce it has to outlast.
    expect(CLOSING_ACK_WINDOW_MS).toBeGreaterThan(300)
  })
})

describe('a composer turning dirty answers a close already in flight', () => {
  beforeEach(() => __resetClosingIntentForTests())

  it('vetoes the LIVE intent, so work appearing after the ack window still refuses', () => {
    const nonce = publishClosingIntent('slot-a')
    expect(nonce).not.toBeNull()
    // The storage responder already ran and found nothing; this is the later keystroke.
    expect(closingIntentVetoed(nonce!)).toBe(false)
    vetoLiveClosingIntent('slot-a', 'composer-7')
    expect(closingIntentVetoed(nonce!)).toBe(true)
  })

  it('no-ops when no close is pending, so a dirty keystroke is not a veto by itself', () => {
    vetoLiveClosingIntent('slot-quiet', 'composer-7')
    const nonce = publishClosingIntent('slot-quiet')
    expect(closingIntentVetoed(nonce!)).toBe(false)
  })

  it('is wired to the dirty transition, not only to the storage responder', () => {
    const HOOK = readFileSync(join(__dirname, '..', 'hooks', 'useSlotComposerRegistration.ts'), 'utf-8')
    expect(HOOK).toContain('vetoLiveClosingIntent(slot, `composer-${id}`)')
    const publish = HOOK.indexOf('publishSlotDirty(id, slot, hasUnsentWork')
    const veto = HOOK.indexOf('vetoLiveClosingIntent(slot,')
    expect(publish).toBeGreaterThan(-1)
    expect(veto).toBeGreaterThan(publish)
  })
})

describe('the pane close-error notice renders where a user can see it', () => {
  it('sits OUTSIDE the agent-picker portal, which mounts only while the dropdown is open', () => {
    const PANE = readFileSync(join(__dirname, '..', 'components', 'ChatPane.tsx'), 'utf-8')
    const notice = PANE.indexOf('testId="chat-pane-close-error"')
    const portal = PANE.indexOf('agentDD.open && agentBtnRect && createPortal(')
    expect(notice).toBeGreaterThan(-1)
    expect(portal).toBeGreaterThan(-1)
    expect(notice).toBeLessThan(portal)
  })

  it('sits beside its sibling upload notice, the pane established locus', () => {
    const PANE = readFileSync(join(__dirname, '..', 'components', 'ChatPane.tsx'), 'utf-8')
    const sibling = PANE.indexOf('testId="chat-pane-upload-error"')
    const notice = PANE.indexOf('testId="chat-pane-close-error"')
    expect(sibling).toBeGreaterThan(-1)
    expect(notice).toBeGreaterThan(sibling)
  })

  it('keeps the No hand-off rationale ADJACENT to the close-error notice', () => {
    // Relocating this notice out of the agent-picker portal once left its rationale
    // behind, orphaned 130 lines away, which satisfies the blocking AUTOSDE rule nowhere.
    const PANE = readFileSync(join(__dirname, '..', 'components', 'ChatPane.tsx'), 'utf-8')
    const lines = PANE.split('\n')
    const notice = lines.findIndex(l => l.includes('testId="chat-pane-close-error"'))
    expect(notice).toBeGreaterThan(-1)
    let open = notice
    while (!lines[open].includes('<ErrorNotice')) open -= 1
    const above = lines.slice(Math.max(0, open - 4), open).join('\n')
    expect(above).toContain('No hand-off')
    expect(above).toMatch(/composer draft/)
  })

  it('leaves no ORPHANED hand-off comment behind, which satisfies nothing', () => {
    const PANE = readFileSync(join(__dirname, '..', 'components', 'ChatPane.tsx'), 'utf-8')
    const lines = PANE.split('\n')
    lines.forEach((line, i) => {
      if (!line.includes('No hand-off')) return
      let end = i
      while (!lines[end].includes('*/')) end += 1
      const next = lines.slice(end + 1).find(l => l.trim().length > 0) || ''
      expect(next, `hand-off comment at line ${i + 1} guards ${next.trim()}`)
        .toContain('<ErrorNotice')
    })
  })

  it('marks only the named slot quiescing, and clears it on release', () => {
    expect(slotIsQuiescing('a')).toBe(false)
    const release = beginSlotQuiesce('a')
    expect(slotIsQuiescing('a')).toBe(true)
    // Per SLOT: a close of one session must not silence a composer in another.
    expect(slotIsQuiescing('b')).toBe(false)
    release()
    expect(slotIsQuiescing('a')).toBe(false)
  })

  it('releases IDEMPOTENTLY, so a stale release cannot un-quiesce a later close', () => {
    const first = beginSlotQuiesce('a')
    first()
    const second = beginSlotQuiesce('a')
    // The first close's `finally` runs again -- a plain delete would clear the second.
    first()
    expect(slotIsQuiescing('a')).toBe(true)
    second()
    expect(slotIsQuiescing('a')).toBe(false)
  })

  it('treats an empty slot key as nothing to quiesce', () => {
    const release = beginSlotQuiesce('')
    expect(slotIsQuiescing('')).toBe(false)
    release()
  })
})
