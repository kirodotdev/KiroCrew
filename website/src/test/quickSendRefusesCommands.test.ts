import { describe, expect, it } from 'vitest'
import { tryQuickSend } from '../lib/quickSend'
import { dispatchIsCommandShaped } from '../app-sdk/protocol/recommendation'

/**
 * One click must never RUN something an agent put in an options menu.
 *
 * The marker guard refuses to strip a prefix that would expose a command, but a label needs
 * no marker to be dangerous: a bare `/clear` chip was not marker-declined, so quick-send
 * fired it and the transcript was erased. The reachable chain is opt-in quick send, an option
 * reaching the menu from content the agent read, and one ordinary user click.
 */
describe('quick send refuses a label that would run rather than say', () => {
  // Quick send on, no modifier, slot idle, nothing picked: the one-click path.
  const ARM = [true, false, false, 0] as const

  function sent(option: string): string | null {
    let out: string | null = null
    tryQuickSend(option, ...ARM, (t: string) => {
      out = t
    })
    return out
  }

  it('sends an ordinary label, so the arm is really armed', () => {
    expect(sent('Merge it now')).toBe('Merge it now')
  })

  it('refuses a bare slash command', () => {
    expect(sent('/clear')).toBeNull()
  })

  it('refuses a bare prompt mention, which substitutes a stored body', () => {
    expect(sent('@deploy')).toBeNull()
  })

  it('refuses a bare provenance opener, which forges an origin', () => {
    expect(sent('[Monitor wake]')).toBeNull()
  })

  it('still SENDS a plan action, which is an affordance the user clicks on purpose', () => {
    // `Go` dispatches to the plan-action endpoint. That is a product feature, not an
    // injection: refusing it would delete a control rather than close a hole.
    expect(sent('Go')).toBe('Go')
  })

  it('still sends a stop word, for the same reason', () => {
    expect(sent('cancel the deploy')).toBe('cancel the deploy')
  })

  it('still refuses a marker-declined label, so the older guard is intact', () => {
    expect(sent('(recommended) /clear')).toBeNull()
  })

  it('reports the same verdict through the shared predicate', () => {
    // One grammar, two consumers: a new dispatch form must register only once.
    expect(dispatchIsCommandShaped('/clear')).toBe(true)
    expect(dispatchIsCommandShaped('Merge it now')).toBe(false)
  })

  it('refuses every leading-trim spelling of the same command', () => {
    // The dispatch side trims these before reading, so a guard that does not is bypassable.
    for (const spelling of ['\u200b/clear', '\ufeff/clear', ' /clear']) {
      expect(sent(spelling)).toBeNull()
    }
  })
})
