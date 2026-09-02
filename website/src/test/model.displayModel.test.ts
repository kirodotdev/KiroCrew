import { describe, it, expect } from 'vitest'
import { displayModel, normalizeModelKey, pinIsWithheld } from '../lib/model'

/** The picker's list is narrowed to what the live session says the account can
 *  run, while the slot keeps whatever model was pinned before. After a plan
 *  downgrade those disagree, and the backend withholds the pin and runs the
 *  session on its default — so the composer must name the model that will
 *  actually run, not the dead pin. */
describe('displayModel', () => {
  const list = [
    { name: 'auto' },
    { name: 'claude-sonnet-5' },
    { name: 'claude-opus-4.8' },
  ]

  it('shows a pin that is still on the list', () => {
    expect(displayModel('claude-sonnet-5', list)).toBe('claude-sonnet-5')
  })

  it('falls back to auto for a pin the list no longer offers', () => {
    expect(displayModel('claude-opus-5', list)).toBe('auto')
  })

  it('returns the list spelling so the row actually highlights', () => {
    // Matching is normalized through the canonical registry (an alias and its
    // provider-prefixed canonical id fold together), but ModelDropdownList
    // highlights on exact `activeModel === m.name`. Returning the caller's
    // spelling would show the model in the chip while checking no row. The
    // list's `claude-opus-4.8` and the pin `global.anthropic.claude-opus-4-8[1m]`
    // both resolve to `opus-4.8-1m`, so the pin displays as the list spelling.
    expect(displayModel('global.anthropic.claude-opus-4-8[1m]', list)).toBe('claude-opus-4.8')
    expect(displayModel('CLAUDE-OPUS-4.8', list)).toBe('claude-opus-4.8')
    expect(displayModel('claude-opus-4.8', list)).toBe('claude-opus-4.8')
  })

  it('does NOT fold a 200K variant onto its 1M sibling (#5339)', () => {
    // `claude-opus-4-8` is the advertised 200K model (`opus-4.8`) while the
    // list's `claude-opus-4.8` is the 1M model (`opus-4.8-1m`). The old
    // dot->dash string fold equated them; routing through the registry keeps
    // the two context-window variants distinct, so a 200K pin against a
    // 1M-only list reads as withheld.
    expect(displayModel('claude-opus-4-8', list)).toBe('auto')
  })

  it('keeps the pin when the list is marked degraded', () => {
    // The blocking case: /api/models is failing, React Query keeps serving the
    // last successful list, and that list predates the account regaining access.
    // It looks healthy by LENGTH while being arbitrarily stale, so only the
    // explicit degraded signal can be trusted. Relabelling here would let the
    // "set default for agent" row persist 'auto' over a pin that is valid.
    expect(displayModel('claude-opus-5', list, true)).toBe('claude-opus-5')
  })

  it('keeps the pin when the list is empty', () => {
    expect(displayModel('claude-opus-5', [])).toBe('claude-opus-5')
  })

  it('does not treat list length as the degraded signal', () => {
    // A live backend advertising only auto genuinely offers only auto, so a pin
    // absent from it IS withheld. Length is not evidence either way — that is
    // what the degraded flag is for.
    expect(displayModel('claude-opus-5', [{ name: 'auto' }])).toBe('auto')
    expect(displayModel('claude-opus-5', [{ name: 'auto' }], true)).toBe('claude-opus-5')
  })

  it('renders an unset or auto slot as auto', () => {
    expect(displayModel('', list)).toBe('auto')
    expect(displayModel('auto', list)).toBe('auto')
    expect(displayModel('default', list)).toBe('auto')
  })
})

/** The backend computes the withhold at spawn, against the live session's own
 *  advertised list, and now carries it in the slots payload (#1819). Reading
 *  that beats re-deriving it from picker-list membership: every filter applied
 *  to `/api/models` was otherwise an entitlement signal. */
describe('displayModel with the backend verdict', () => {
  const list = [
    { name: 'auto' },
    { name: 'claude-sonnet-5' },
    { name: 'claude-opus-4.8' },
  ]

  it('shows auto when the backend says the pin is withheld', () => {
    // Even though the list DOES offer it: the verdict comes from the live
    // session, the list is a separate fetch that can be wider.
    expect(displayModel('claude-sonnet-5', list, false, true)).toBe('auto')
  })

  it('honours a withheld verdict while the list is degraded', () => {
    // The degraded flag says the LIST cannot be trusted. The verdict does not
    // come from the list, so it is unaffected — and it is not stale either: the
    // withhold is applied per spawn from the same advertised set, so while the
    // session lives the verdict describes exactly what that session does.
    expect(displayModel('claude-sonnet-5', list, true, true)).toBe('auto')
  })

  it('shows a pin the backend allows even when the list omits it', () => {
    // The conflation this fixes: `/api/models` is narrowed by filters that have
    // nothing to do with entitlement (deprecation today, curation or dedup
    // later), so an absent row is not evidence the account lost the model. With
    // a verdict, absence no longer relabels a runnable pin as 'auto'.
    expect(displayModel('claude-opus-4.6-1m', list, false, false)).toBe('claude-opus-4.6-1m')
  })

  it('still returns the list spelling when the verdict allows a listed pin', () => {
    // A verdict must not cost the row highlight: matching stays normalized and
    // the list's own spelling is what ModelDropdownList compares against.
    expect(displayModel('global.anthropic.claude-opus-4-8[1m]', list, false, false)).toBe(
      'claude-opus-4.8',
    )
  })

  it('falls back to membership when there is no verdict yet', () => {
    // Unknown is the absence of an answer, not a denial: a slot that has never
    // spawned a session carries null, and behaviour must be exactly as before.
    for (const unknown of [null, undefined]) {
      expect(displayModel('claude-opus-5', list, false, unknown)).toBe('auto')
      expect(displayModel('claude-sonnet-5', list, false, unknown)).toBe('claude-sonnet-5')
      expect(displayModel('claude-opus-5', list, true, unknown)).toBe('claude-opus-5')
    }
  })

  it('never names a model for an unpinned slot, whatever the verdict', () => {
    expect(displayModel('', list, false, false)).toBe('auto')
    expect(displayModel('auto', list, false, false)).toBe('auto')
  })
})

describe('pinIsWithheld', () => {
  it('is true when a real pin displays as auto', () => {
    expect(pinIsWithheld('claude-opus-5', 'auto')).toBe(true)
  })

  it('is false for a mere spelling difference', () => {
    // displayModel returns the list's spelling, so pin and shown can differ as
    // strings while naming the same model. Treating that as withheld would
    // disable the pin row for a perfectly usable model. A provider-prefixed
    // canonical id and its bare alias both resolve to `opus-4.8-1m`.
    expect(pinIsWithheld('global.anthropic.claude-opus-4-8[1m]', 'claude-opus-4.8')).toBe(false)
    expect(pinIsWithheld('CLAUDE-OPUS-4.8', 'claude-opus-4.8')).toBe(false)
  })

  it('is false when nothing is pinned', () => {
    expect(pinIsWithheld('', 'auto')).toBe(false)
    expect(pinIsWithheld('auto', 'auto')).toBe(false)
  })

  it('is false when the pin is displayed as itself', () => {
    expect(pinIsWithheld('claude-sonnet-5', 'claude-sonnet-5')).toBe(false)
  })
})

describe('normalizeModelKey', () => {
  it('folds the auto/default synonyms and keeps unset empty', () => {
    expect(normalizeModelKey(' auto ')).toBe('auto')
    expect(normalizeModelKey('default')).toBe('auto')
    expect(normalizeModelKey('DEFAULT')).toBe('auto')
    expect(normalizeModelKey('')).toBe('')
    expect(normalizeModelKey('   ')).toBe('')
  })

  it('routes a registry alias / canonical key / provider id to one canonical key', () => {
    // An alias, the canonical key itself, and the claude_code provider id all
    // fold to the same canonical key regardless of case.
    expect(normalizeModelKey('claude-opus-4.8')).toBe('opus-4.8-1m')
    expect(normalizeModelKey('Claude-Opus-4.8')).toBe('opus-4.8-1m')
    expect(normalizeModelKey('opus-4.8-1m')).toBe('opus-4.8-1m')
    expect(normalizeModelKey('opus')).toBe('opus-4.8-1m')
    expect(normalizeModelKey('global.anthropic.claude-opus-4-8[1m]')).toBe('opus-4.8-1m')
    // The "fold a provider/partition prefix" half of #5339: a regional profile
    // id that is not itself a registry entry still folds after the prefix peel.
    expect(normalizeModelKey('us.anthropic.claude-opus-4-8[1m]')).toBe('opus-4.8-1m')
  })

  it('keeps distinct 200K / 1M context variants apart (#5339)', () => {
    // The old dot->dash fold made both of these `claude-opus-4-8`, equating a
    // 200K model with a 1M one. The registry lists them as separate entries.
    expect(normalizeModelKey('claude-opus-4-8')).toBe('opus-4.8') // 200K
    expect(normalizeModelKey('claude-opus-4.8')).toBe('opus-4.8-1m') // 1M
    expect(normalizeModelKey('claude-opus-4-8')).not.toBe(normalizeModelKey('claude-opus-4.8'))
  })

  it('keeps kiro-distinct models apart via the acp-first fold (#5339 Design)', () => {
    // The claude_code index aliases these onto Sonnet/Opus 4.8 for dropdown
    // dedup, but kiro serves them as DISTINCT real models. Resolving the acp
    // index first keeps them apart, so the shared fold cannot equate e.g. a
    // Haiku pin with Sonnet 4.6 (a real 1M->200K swap isModelDowngrade must catch).
    expect(normalizeModelKey('claude-haiku-4.5')).toBe('haiku-4.5')
    expect(normalizeModelKey('claude-sonnet-4.5')).toBe('sonnet-4.5')
    expect(normalizeModelKey('claude-sonnet-4')).toBe('sonnet-4')
    expect(normalizeModelKey('claude-opus-4.6')).toBe('opus-4.6-1m')
    expect(normalizeModelKey('claude-sonnet-4.6')).toBe('sonnet-4.6-1m')
    // None of the kiro-distinct models collapse onto their claude_code fold target.
    expect(normalizeModelKey('claude-haiku-4.5')).not.toBe(normalizeModelKey('claude-sonnet-4.6'))
    expect(normalizeModelKey('claude-opus-4.6')).not.toBe(normalizeModelKey('claude-opus-4.8'))
    // acp-only canonical keys resolve to themselves.
    expect(normalizeModelKey('haiku-4.5')).toBe('haiku-4.5')
    expect(normalizeModelKey('opus-4.6-1m')).toBe('opus-4.6-1m')
  })

  it('passes an unregistered id through the lossless string fold', () => {
    // GPT/DeepSeek/Qwen and future models are absent from the (Anthropic-only)
    // registry, so they keep the historical trim/lowercase/dot->dash behavior.
    expect(normalizeModelKey('GPT-5.6')).toBe('gpt-5-6')
    expect(normalizeModelKey('deepseek-3.2')).toBe('deepseek-3-2')
    expect(normalizeModelKey('claude-opus-5')).toBe('claude-opus-5')
  })
})
