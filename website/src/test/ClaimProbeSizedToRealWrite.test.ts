/**
 * The storage probe must COST at least as much as the write it vouches for.
 *
 * `storageAcceptsClaims()` is the fail-closed guard's only evidence when no claim is
 * found. Probing with a 28-byte key/value pair let it pass in a near-full band where the
 * real claim AND the failure record both failed — window B then read the slot clean and a
 * confirmed close destroyed window A's unsent draft, with no recovery path.
 *
 * These assert the OBSERVED write, not the budget constant: a correct constant with the
 * probe still writing the bare pair is the exact regression that shipped.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  __resetSlotDirtyForTests,
  anyWindowSlotDirty,
  claimFailureKey,
  WORST_CLAIM_WRITE_BYTES,
} from '../utils/slotDirtyBeacon'
import { nextComposerId } from '../utils/slotComposerRegistry'

const PREFIX = 'mc-slot-dirty:'
const SLOT = 'chat-1281-1785676802'

/** Bytes `publishSlotDirty` charges for the largest claim shape. */
const realClaimWrite = (composerId: string) =>
  `${PREFIX}${composerId}`.length + JSON.stringify({ s: SLOT, t: Date.now(), u: 1 as const }).length

/** Bytes the failure record charges. */
const realFailureWrite = (composerId: string) =>
  claimFailureKey(composerId).length + JSON.stringify({ f: Date.now() }).length

/** Every key+value pair the probe writes under our prefix, by total byte cost. */
function probeWrites(): number[] {
  const spy = vi.spyOn(Storage.prototype, 'setItem')
  __resetSlotDirtyForTests()
  spy.mockClear()
  // No claims exist, so the guard consults the probe -- the only path that writes here.
  anyWindowSlotDirty('slot-1')
  const costs = spy.mock.calls
    .filter(([k]) => typeof k === 'string' && k.startsWith(PREFIX))
    .map(([k, v]) => String(k).length + String(v ?? '').length)
  spy.mockRestore()
  return costs
}

describe('the claim probe is sized to a real write', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetSlotDirtyForTests()
  })
  afterEach(() => {
    vi.restoreAllMocks()
    localStorage.clear()
  })

  it('actually writes at least one probe', () => {
    // Guards every assertion below: a probe that never writes would pass them vacuously.
    expect(probeWrites().length).toBeGreaterThan(0)
  })

  it('writes at least as many bytes as a real claim for a live composer', () => {
    const need = realClaimWrite(nextComposerId())
    expect(Math.max(...probeWrites())).toBeGreaterThanOrEqual(need)
  })

  it('writes at least as many bytes as a real failure record', () => {
    const need = realFailureWrite(nextComposerId())
    expect(Math.max(...probeWrites())).toBeGreaterThanOrEqual(need)
  })

  it('writes far more than the bare key/value pair it replaced', () => {
    expect(Math.max(...probeWrites())).toBeGreaterThan(PREFIX.length * 2 * 2)
  })

  it('keeps the probe on the bare prefix so every read path still skips it', () => {
    // A probe under a failure-shaped key would be READ as a live failure record if its
    // removal ever failed, holding every slot shut. The value carries the bytes instead.
    const keys = new Set<string>()
    const spy = vi.spyOn(Storage.prototype, 'setItem')
    __resetSlotDirtyForTests()
    anyWindowSlotDirty('slot-1')
    for (const [k] of spy.mock.calls) if (String(k).startsWith(PREFIX)) keys.add(String(k))
    spy.mockRestore()
    expect([...keys]).toEqual([PREFIX])
  })

  it('keeps the budget constant consistent with the writers', () => {
    expect(WORST_CLAIM_WRITE_BYTES).toBeGreaterThanOrEqual(realClaimWrite(nextComposerId()))
    expect(WORST_CLAIM_WRITE_BYTES).toBeGreaterThanOrEqual(realFailureWrite(nextComposerId()))
  })
})
