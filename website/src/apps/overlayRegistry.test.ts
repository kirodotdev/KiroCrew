import { describe, expect, it } from 'vitest'

import {
  BUILTIN_OVERLAY_REGISTRY,
  HOST_OVERLAY_SLOTS,
  getBuiltinOverlay,
  hasBuiltinOverlay,
  isHostOverlaySlot,
} from './overlayRegistry'

/**
 * The registry is the only thing standing between a manifest-supplied string and
 * a component the host renders, so its lookups are asserted directly.
 */

describe('isHostOverlaySlot', () => {
  it('accepts the slots the host actually offers', () => {
    for (const slot of HOST_OVERLAY_SLOTS) expect(isHostOverlaySlot(slot)).toBe(true)
  })

  it('rejects an unknown slot name', () => {
    // A slot is a position the host deliberately opens for replacement, so an app
    // naming anything else must not be able to displace host UI.
    expect(isHostOverlaySlot('sidebar')).toBe(false)
    expect(isHostOverlaySlot('')).toBe(false)
    expect(isHostOverlaySlot('Quick-Search')).toBe(false)
  })

  it('rejects inherited Object.prototype names', () => {
    // `includes` is an own-value scan so this holds, and it must keep holding if
    // the list is ever reshaped into an object.
    expect(isHostOverlaySlot('constructor')).toBe(false)
    expect(isHostOverlaySlot('toString')).toBe(false)
  })
})

describe('overlay component lookup', () => {
  it('resolves a bundled overlay id', () => {
    expect(hasBuiltinOverlay('command-bar')).toBe(true)
    expect(getBuiltinOverlay('command-bar')).toBe(BUILTIN_OVERLAY_REGISTRY['command-bar'])
  })

  it('reports an unbundled id as absent instead of resolving it', () => {
    expect(hasBuiltinOverlay('not-bundled')).toBe(false)
    expect(getBuiltinOverlay('not-bundled')).toBeUndefined()
  })

  it('never resolves an inherited Object.prototype name to a component', () => {
    // Ids arrive from installed app manifests and the slug grammar admits
    // `constructor`: an `in`-based membership test would satisfy the lookup and
    // hand the host `Object` to render as a component.
    for (const name of ['constructor', 'toString', 'valueOf', '__proto__', 'hasOwnProperty']) {
      expect(hasBuiltinOverlay(name)).toBe(false)
      expect(getBuiltinOverlay(name)).toBeUndefined()
    }
  })
})
