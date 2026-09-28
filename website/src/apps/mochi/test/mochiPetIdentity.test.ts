/**
 * Pet-name resolution and the mount-time mood seed.
 *
 * Both bugs these pin have the same shape: a value the BACKEND resolves was
 * re-derived in the renderer with a hardcoded fallback that skipped a rung.
 *
 *  - `petName` defaults to `""`, meaning "use the active avatar's own name"
 *    (settings.py / soul_loader.BUILTIN_PET_NAMES). Every renderer read the raw
 *    field with `|| 'Mochi'`, so a ghost user was told to "Ask Mochi" to watch a
 *    page while the pet introduced itself as Kiro.
 *  - `/pet-state` answers `{state, mood}`, but the bridge returned only the
 *    state, so nothing could seed a mood on mount. Moods self-clear after a few
 *    seconds, so the chat title bar effectively never showed one.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { DEFAULT_PET_NAME, resolvePetName } from '../builtinPacks'

describe('resolvePetName', () => {
  it('prefers the name the user typed', () => {
    expect(resolvePetName({ petName: 'Tofu', activeAppearance: 'kiro-ghost' })).toBe('Tofu')
  })

  it('falls back to the ACTIVE avatar, not to the cat', () => {
    // The whole point: an unnamed ghost is Kiro, and mirrors
    // soul_loader.BUILTIN_PET_NAMES so chat and UI agree.
    expect(resolvePetName({ petName: '', activeAppearance: 'kiro-ghost' })).toBe('Kiro')
    expect(resolvePetName({ petName: '', activeAppearance: 'default-mochi' })).toBe('Mochi')
  })

  it('falls back to the default for a user pack and for no config', () => {
    // An imported pack's meta.name is a design label, not an address.
    expect(resolvePetName({ activeAppearance: 'pack-abc123' })).toBe(DEFAULT_PET_NAME)
    expect(resolvePetName(undefined)).toBe(DEFAULT_PET_NAME)
  })

  it('trims, so a pasted name with whitespace is not treated as custom', () => {
    expect(resolvePetName({ petName: '   ', activeAppearance: 'kiro-ghost' })).toBe('Kiro')
    expect(resolvePetName({ petName: ' Tofu ' })).toBe('Tofu')
  })
})

describe('getPetStateInfo', () => {
  let bridge: typeof import('../panel/panelBridge')

  beforeEach(async () => {
    vi.resetModules()
    bridge = await import('../panel/panelBridge')
  })

  it('returns the mood alongside the state', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: true, json: async () => ({ state: 'working', mood: 'busy' }) })),
    )
    await expect(bridge.getPetStateInfo()).resolves.toEqual({ state: 'working', mood: 'busy' })
  })

  it('stands in the manager\u2019s initial mood when the key is absent', async () => {
    // An older backend answers `{state}` only; 'neutral' is what the manager
    // itself starts at, so it is the honest stand-in rather than ''.
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: true, json: async () => ({ state: 'idle' }) })),
    )
    await expect(bridge.getPetStateInfo()).resolves.toEqual({ state: 'idle', mood: 'neutral' })
  })

  it('degrades to cold-start values instead of throwing', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false })))
    await expect(bridge.getPetStateInfo()).resolves.toEqual({ state: 'offline', mood: 'neutral' })
  })

  it('keeps getPetState string-compatible for its existing callers', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: true, json: async () => ({ state: 'thinking', mood: 'curious' }) })),
    )
    await expect(bridge.getPetState()).resolves.toBe('thinking')
  })
})
