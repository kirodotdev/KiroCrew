/**
 * The break-nudge lookup: the seam between what the backend sends and what the
 * companion says out loud.
 *
 * This is worth pinning because the failure is silent and user-facing. The backend
 * sends a KEY (`break.water.3`); if the lookup or the catalogue drifts from it, the
 * companion either says nothing or — the version this replaced — says the literal
 * string "break.water.3" to someone trying to work.
 */
import { describe, it, expect } from 'vitest'
import { nudgeTextFor } from '../apps/crew-companion/nudgeKeys'

/** Exactly the shape `BREAK_NUDGES` in reminders.py produces. */
const KINDS = ['water', 'stretch', 'distance', 'breathe'] as const
const VARIANTS = [1, 2, 3, 4, 5] as const

describe('nudgeTextFor', () => {
  it('resolves every key the backend can emit', () => {
    for (const kind of KINDS) {
      for (const n of VARIANTS) {
        const text = nudgeTextFor(`break.${kind}.${n}`)
        expect(text, `break.${kind}.${n} must resolve`).toBeTruthy()
      }
    }
  })

  it('never returns a raw key as the text', () => {
    // The exact regression this module exists to prevent: a lookup that "works" by
    // handing the key back would pass a truthiness check and still be a bug on
    // screen.
    for (const kind of KINDS) {
      for (const n of VARIANTS) {
        const key = `break.${kind}.${n}`
        expect(nudgeTextFor(key)).not.toBe(key)
        expect(nudgeTextFor(key)).not.toContain('crewCompanion')
      }
    }
  })

  it('returns a distinct phrasing for each variant of a kind', () => {
    // Five variants exist so a nudge does not become wallpaper. Duplicates would
    // defeat that silently, since rotation would still "work".
    for (const kind of KINDS) {
      const seen = VARIANTS.map((n) => nudgeTextFor(`break.${kind}.${n}`))
      expect(new Set(seen).size, `${kind} variants must all differ`).toBe(VARIANTS.length)
    }
  })

  it('stays silent on a key it does not ship', () => {
    // Silence beats guessing: an unknown key means backend and catalogue drifted,
    // and a wrong sentence is worse than none.
    expect(nudgeTextFor('break.water.6')).toBeNull()
    expect(nudgeTextFor('break.coffee.1')).toBeNull()
    expect(nudgeTextFor('break.water')).toBeNull()
    expect(nudgeTextFor('')).toBeNull()
  })

  it('refuses a same-shaped key from another namespace', () => {
    // Three dotted segments is not enough to be one of ours; the prefix is checked
    // so an unrelated key cannot borrow this table.
    expect(nudgeTextFor('nudge.water.1')).toBeNull()
    expect(nudgeTextFor('apps.water.1')).toBeNull()
  })
})
