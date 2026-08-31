import { describe, expect, it } from 'vitest'

import {
  dispatchIsCommandShaped,
  dispatchWouldPromote,
  markerDeclined,
  splitRecommendation,
} from '../app-sdk/protocol/recommendation'
import { tryQuickSend } from '../lib/quickSend'

/** Drives the real chokepoint and reports whether it dispatched. */
function wouldSend(option: string): boolean {
  let sent: string | null = null
  tryQuickSend(option, true, false, false, 0, text => {
    sent = text
  })
  return sent !== null
}

/**
 * `$skill` is a dispatch protocol on the dashboard, alongside `/command` and `@file` -- the
 * composer's own placeholder lists all three. So a label opening with it runs a local skill
 * body rather than saying something, and the fence has to hold it exactly as it holds the
 * other two.
 */
describe('the skill sigil is fenced like every other dispatch protocol', () => {
  it('treats a bare skill label as command-shaped', () => {
    expect(dispatchIsCommandShaped('$deploy')).toBe(true)
  })

  it('treats a skill label as promoting, so a marker is never stripped off it', () => {
    expect(dispatchWouldPromote('$deploy')).toBe(true)
  })

  it('keeps the marker on a marked skill label', () => {
    // Stripping it would expose `$deploy` as the dispatched string.
    const split = splitRecommendation('(recommended) $deploy')
    expect(split.label).toBe('(recommended) $deploy')
    expect(split.hasMarker).toBe(false)
    expect(markerDeclined('(recommended) $deploy')).toBe(true)
  })

  it('refuses to quick-send a marked skill label', () => {
    expect(wouldSend('(recommended) $deploy')).toBe(false)
  })

  it('refuses to quick-send a bare skill label', () => {
    expect(wouldSend('$deploy')).toBe(false)
  })

  it('still quick-sends an ordinary label, so the fence is not a blanket refusal', () => {
    expect(wouldSend('Run the tests again')).toBe(true)
  })

  it('fences a dollar token anywhere in the label, not only at the front', () => {
    // The expander resolves `$name` anywhere, so the fence matches it anywhere.
    expect(dispatchIsCommandShaped('Refund the $40 charge')).toBe(true)
    expect(dispatchIsCommandShaped('Refund the charge')).toBe(false)
  })
})
