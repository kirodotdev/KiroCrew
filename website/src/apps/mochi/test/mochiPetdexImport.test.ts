/**
 * Petdex import convention + built-in pack resolution.
 *
 * Two things are pinned here.
 *
 * 1. **The petdex grid is derived from pixels, not assumed.** The published
 *    assets come in two sizes (9-row and 11-row) and the upstream open-source
 *    client hard-codes nine, so a constant would mis-slice one of them. When the
 *    sheet does not divide evenly, the prefill must decline to guess.
 *
 * 2. **One identity key.** `activeAppearance` is the whole answer -- the pack id
 *    IS the character, and it drives the art, the persona, and the default pet
 *    name together. The original had a second key (`pet.character`) and migrated
 *    it away; this port briefly re-created the split as `avatar`, which let the
 *    pet render an imported pack while its prompt described the built-in cat.
 *    `resolveActivePackId` is the single resolver every surface uses.
 */
import { describe, expect, it } from 'vitest'

import {
  BUILTIN_GHOST_ID,
  BUILTIN_MOCHI_ID,
  builtinPackDetail,
  builtinPackMetas,
  isBuiltinPack,
  resolveActivePackId,
} from '../builtinPacks'
import {
  PETDEX_FRAME_H,
  PETDEX_FRAME_W,
  PETDEX_ROWS,
  PETDEX_SLOT_ROWS,
  derivePetdexGrid,
  petdexPrefill,
  type PetdexPet,
} from '../petdexImport'
import { REQUIRED_STATES } from '../src/shared/appearanceTypes'

const pet: PetdexPet = {
  slug: 'wangcai',
  meta: { id: 'wangcai', displayName: 'Wangcai', description: 'A calm ragdoll cat.' },
  imageMime: 'image/webp',
  imageBase64: 'AAAA',
  source: 'petdex.dev',
}

describe('derivePetdexGrid', () => {
  it('accepts the documented 8x9 community sheet', () => {
    const grid = derivePetdexGrid(1536, 1872)
    expect(grid).toMatchObject({ cols: 8, rows: 9, matchesConvention: true })
    expect(grid.frameWidth).toBe(PETDEX_FRAME_W)
    expect(grid.frameHeight).toBe(PETDEX_FRAME_H)
  })

  it('accepts the taller curated sheet as extra rows, not a different frame box', () => {
    // 1536x2288 was observed on a featured pet carrying spriteVersionNumber 2.
    // 2288 / 208 = 11, so the frame box is unchanged and two rows are simply
    // beyond the nine the convention names.
    const grid = derivePetdexGrid(1536, 2288)
    expect(grid).toMatchObject({ cols: 8, rows: 11, matchesConvention: true })
  })

  it('declines a sheet that does not divide into the frame box', () => {
    expect(derivePetdexGrid(1000, 1000).matchesConvention).toBe(false)
    expect(derivePetdexGrid(0, 0).matchesConvention).toBe(false)
  })

  it('declines a sheet with too few rows to hold the convention', () => {
    // Evenly divisible, but only 4 rows: the row indices would point past the end.
    expect(derivePetdexGrid(1536, PETDEX_FRAME_H * 4).matchesConvention).toBe(false)
  })
})

describe('PETDEX_SLOT_ROWS', () => {
  it('covers every required Mochi state', () => {
    for (const state of REQUIRED_STATES) {
      expect(PETDEX_SLOT_ROWS[state], `no default row for ${state}`).toBeTypeOf('number')
    }
  })

  it('points only at rows the convention actually names', () => {
    for (const [slot, row] of Object.entries(PETDEX_SLOT_ROWS)) {
      expect(row, `${slot} row out of range`).toBeLessThan(PETDEX_ROWS.length)
      expect(row).toBeGreaterThanOrEqual(0)
    }
  })

  it('maps walking to running-right, leaving running-left to flipX', () => {
    // The pet mirrors itself to walk the other way, so consuming the left row
    // would spend an animation on something flipX already provides.
    const right = PETDEX_ROWS.find((r) => r.id === 'running-right')!
    const left = PETDEX_ROWS.find((r) => r.id === 'running-left')!
    expect(PETDEX_SLOT_ROWS.walking).toBe(right.index)
    expect(Object.values(PETDEX_SLOT_ROWS)).not.toContain(left.index)
  })
})

describe('petdexPrefill', () => {
  it('fills geometry, metadata, and the row mapping for a conforming sheet', () => {
    const prefill = petdexPrefill(pet, 1536, 1872)
    expect(prefill.name).toBe('Wangcai')
    expect(prefill.author).toBe('petdex.dev')
    expect(prefill.description).toBe('A calm ragdoll cat.')
    expect(prefill.frameWidth).toBe(PETDEX_FRAME_W)
    expect(prefill.frameHeight).toBe(PETDEX_FRAME_H)
    expect(prefill.imageUri.startsWith('data:image/webp;base64,')).toBe(true)
    for (const state of REQUIRED_STATES) {
      expect(prefill.rowAssignments[state]).toBeTypeOf('number')
    }
  })

  it('supplies NO geometry and NO mapping when the grid is unconfirmed', () => {
    // A mapping computed from a grid we could not verify would point at the
    // wrong art with full confidence. Zero geometry hands the importer back to
    // its own detection and leaves every dropdown for the user.
    const prefill = petdexPrefill(pet, 1000, 1000)
    expect(prefill.frameWidth).toBe(0)
    expect(prefill.frameHeight).toBe(0)
    expect(prefill.rowAssignments).toEqual({})
    expect(prefill.imageUri).not.toBe('')
  })

  it('falls back to the slug when pet.json has no display name', () => {
    const bare: PetdexPet = { ...pet, meta: {} }
    expect(petdexPrefill(bare, 1536, 1872).name).toBe('wangcai')
  })
})

describe('built-in packs', () => {
  it('registers both avatars, default first', () => {
    expect(builtinPackMetas().map((m) => m.id)).toEqual([BUILTIN_MOCHI_ID, BUILTIN_GHOST_ID])
    expect(isBuiltinPack(BUILTIN_GHOST_ID)).toBe(true)
    expect(isBuiltinPack('some-user-pack')).toBe(false)
  })

  it('gives the ghost every required state with real Lottie content', () => {
    const detail = builtinPackDetail(BUILTIN_GHOST_ID)!
    expect(detail).not.toBeNull()
    for (const state of REQUIRED_STATES) {
      const anim = detail.animations[state]
      expect(anim, `ghost has no ${state}`).toBeDefined()
      expect(anim.format).toBe('lottie')
      // Real Lottie, not a placeholder: the fields the renderer needs.
      const parsed = JSON.parse(anim.content)
      expect(parsed).toHaveProperty('layers')
      expect(parsed).toHaveProperty('fr')
    }
  })

  it('omits the peek poses rather than substituting a float', () => {
    // They are optional and the resolver falls back to idle/thinking; a peek is
    // a specific half-off-screen drawing none of the delivered clips is.
    const detail = builtinPackDetail(BUILTIN_GHOST_ID)!
    expect(detail.animations.peeking).toBeUndefined()
    expect(detail.animations.peekThinking).toBeUndefined()
  })

  it('returns null for an unknown pack instead of an empty detail', () => {
    expect(builtinPackDetail('nope')).toBeNull()
  })
})

describe('resolveActivePackId', () => {
  it('returns the stored pack id, built-in or imported', () => {
    expect(resolveActivePackId({ activeAppearance: BUILTIN_GHOST_ID })).toBe(BUILTIN_GHOST_ID)
    expect(resolveActivePackId({ activeAppearance: 'user-pack' })).toBe('user-pack')
  })

  it('falls back to the cat pack for an absent or empty value', () => {
    // Matches settings.py, which normalises an empty value to the default pack
    // on write: rendering the default character beats rendering nothing while a
    // config is mid-write.
    expect(resolveActivePackId({ activeAppearance: '' })).toBe(BUILTIN_MOCHI_ID)
    expect(resolveActivePackId({})).toBe(BUILTIN_MOCHI_ID)
    expect(resolveActivePackId(null)).toBe(BUILTIN_MOCHI_ID)
  })

  it('REGRESSION: there is no second key that could disagree with it', () => {
    // The two-key era let `avatar` and `activeAppearance` name different
    // characters. A stray legacy key must be inert here -- the backend migrates
    // it, and nothing in the renderer may start reading it again.
    const withLegacy = { activeAppearance: 'user-pack', avatar: 'ghost' } as {
      activeAppearance: string
    }
    expect(resolveActivePackId(withLegacy)).toBe('user-pack')
  })
})
