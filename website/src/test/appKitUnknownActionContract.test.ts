/**
 * The app-kit compatibility contract, pinned against the PUBLISHED parser.
 *
 * `docs/app-kit/getting-started.md` states that `[OPTION-ACTIONS:]` is a closed enum and
 * that a renderer must treat an action it does not recognise as absent: drop it, render
 * nothing, dispatch nothing. That rule is what makes adding a member later a non-breaking
 * change, so it is load-bearing for every third-party renderer — and a doc sentence cannot
 * hold a code contract on its own.
 *
 * These cases feed FUTURE-shaped markers through `parseOptions` exactly as an app would, and
 * assert nothing reaches the caller. If a later change starts passing unknown actions
 * through, this fails rather than the docs quietly becoming false.
 */
import { describe, expect, it } from 'vitest'

import { parseOptions } from '../app-sdk/protocol/options'

describe('an unrecognised option action reaches no renderer', () => {
  it('drops a future action outright', () => {
    const parsed = parseOptions('Done.\n[OPTION-ACTIONS: archive=Archive this]')
    expect(parsed.action).toBeNull()
  })

  it('drops it without swallowing the prose around it', () => {
    // A renderer that lost the message while dropping the marker would be worse than one
    // that rendered the marker raw.
    const parsed = parseOptions('Here is the answer.\n[OPTION-ACTIONS: archive=Archive this]')
    expect(parsed.text).toContain('Here is the answer.')
    expect(parsed.text).not.toContain('OPTION-ACTIONS')
  })

  it('still yields the KNOWN member when a future one rides alongside it', () => {
    // The forward-compatibility direction that matters: a newer agent may name a member this
    // build has never heard of, and the one it does understand must survive that.
    const parsed = parseOptions('[OPTION-ACTIONS: archive=Nope | close=Yes]')
    expect(parsed.action?.action).toBe('close')
    expect(parsed.action?.label).toBe('Yes')
  })

  it('drops an action whose name merely CONTAINS a known one', () => {
    // `close` is a closed enum member, not a prefix: a substring match would dispatch a
    // future `close-all` as an ordinary close and destroy more than the user asked.
    for (const spelling of ['close-all', 'closeall', 'unclose']) {
      expect(parseOptions(`[OPTION-ACTIONS: ${spelling}=Go]`).action).toBeNull()
    }
  })

  it('drops an entry carrying no label, so a chip can never render nameless', () => {
    expect(parseOptions('[OPTION-ACTIONS: close=]').action).toBeNull()
  })

  it('is unaffected by the content-choice marker sharing the line', () => {
    // The two markers never parse each other's syntax, so an unknown ACTION must not
    // suppress the ordinary choices offered beside it.
    const parsed = parseOptions('[OPTIONS: a | b]\n[OPTION-ACTIONS: archive=Archive]')
    expect(parsed.action).toBeNull()
    expect(parsed.options.length).toBeGreaterThan(0)
  })
})
