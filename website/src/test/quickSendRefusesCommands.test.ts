import { describe, expect, it } from 'vitest'
import { tryQuickSend } from '../lib/quickSend'
import { dispatchIsCommandShaped } from '../app-sdk/protocol/dispatchShape'

/**
 * One click must never RUN something an agent put in an options menu.
 *
 * A bare `/clear` chip fired on quick send and erased the transcript. The reachable
 * chain is opt-in quick send, an option reaching the menu from content the agent read,
 * and one ordinary user click.
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

  it('sends a bare provenance opener, which runs nothing', () => {
    // A provenance opener mislabels a message's ORIGIN. It is the producer's own visible
    // text and the user reads it before clicking.
    expect(sent('[Monitor wake]')).toBe('[Monitor wake]')
  })

  it('sends a plan action, which is an affordance the user clicks on purpose', () => {
    // Refusing it would delete a control rather than close a hole.
    expect(sent('Go')).toBe('Go')
  })

  it('sends a stop word, for the same reason', () => {
    expect(sent('cancel the deploy')).toBe('cancel the deploy')
  })

  it('refuses an embedded skill token, the one form a leading test cannot see', () => {
    // The expander resolves `$name` ANYWHERE in the dispatched string.
    expect(sent('Run the $deploy skill')).toBeNull()
  })

  it('sends a price, which the backend does not count as a skill attempt', () => {
    expect(sent('Approve $40 spend')).toBe('Approve $40 spend')
    expect(sent('$40 is fine')).toBe('$40 is fine')
  })

  it('refuses a digit-led skill token that holds a letter', () => {
    expect(sent('Run $5whys')).toBeNull()
  })

  it('sends an upper-case $ word, which the expander pattern does not match', () => {
    expect(sent('Set $PATH first')).toBe('Set $PATH first')
  })

  it('refuses a bare paste token, which the send path expands into the staged block', () => {
    expect(sent('[ Paste #1 · 3 lines ]')).toBeNull()
  })

  it('refuses a paste token inside prose, since expansion is not anchored', () => {
    expect(sent('Send [ Paste #2 · 12 lines ] now')).toBeNull()
  })

  it('sends prose that only mentions pasting', () => {
    expect(sent('Paste #1 into the doc')).toBe('Paste #1 into the doc')
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

  it('falls through to the ordinary pick, so the caller puts the label in the draft', () => {
    expect(tryQuickSend('/clear', ...ARM, () => {})).toBe(false)
  })
})
