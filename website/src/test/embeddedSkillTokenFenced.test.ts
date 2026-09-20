import { describe, expect, it } from 'vitest'

import {
  dispatchIsCommandShaped,
  dispatchWouldPromote,
  markerDeclined,
  splitRecommendation,
} from '../app-sdk/protocol/recommendation'
import { tryQuickSend } from '../lib/quickSend'

/**
 * The backend resolves `$name` tokens ANYWHERE in a message, not only at the start, so a
 * leading-sigil test cannot see the ones that matter: `Run the $deploy skill` reads as ordinary
 * prose and loads a local skill body on dispatch. The fence has to match the same token shape
 * the expander does.
 */
function wouldSend(option: string): boolean {
  let sent: string | null = null
  tryQuickSend(option, true, false, false, 0, text => {
    sent = text
  })
  return sent !== null
}

describe('an embedded skill token is fenced, not just a leading one', () => {
  it('treats a mid-string skill token as command-shaped', () => {
    expect(dispatchIsCommandShaped('Run the $deploy skill')).toBe(true)
  })

  it('treats it as promoting, so a marker is never stripped off it', () => {
    expect(dispatchWouldPromote('Run the $deploy skill')).toBe(true)
  })

  it('keeps the marker on a label whose skill token is embedded', () => {
    const split = splitRecommendation('(recommended) Run the $deploy skill')
    expect(split.label).toBe('(recommended) Run the $deploy skill')
    expect(split.hasMarker).toBe(false)
    expect(markerDeclined('(recommended) Run the $deploy skill')).toBe(true)
  })

  it('refuses to quick-send an embedded skill token', () => {
    expect(wouldSend('Run the $deploy skill')).toBe(false)
  })

  it('fences a token at the very end of the label too', () => {
    expect(dispatchIsCommandShaped('Deploy with $prod-release')).toBe(true)
  })

  it('fences a digit-leading token, because the expander accepts one', () => {
    // The expander's token shape allows a digit first, so `$40` IS a candidate it would try to
    // resolve. A fence narrower than the thing it fences is the defect this round fixed.
    expect(dispatchIsCommandShaped('Refund the $40 charge')).toBe(true)
    expect(wouldSend('Refund the $40 charge')).toBe(false)
  })

  it('leaves an escaped or doubled sigil alone, as the expander does', () => {
    expect(dispatchIsCommandShaped('Print a literal $$deploy token')).toBe(false)
  })

  it('still sends ordinary prose, so the fence is not a blanket refusal', () => {
    expect(wouldSend('Run every check again from the beginning')).toBe(true)
  })
})
