/**
 * AnimationResolver — resolving a (state, mood) pair to a renderable animation
 * source from an appearance pack.
 *
 * The class had no coverage: PetAvatar / GalleryPanel / ColorCustomizerPanel drive it
 * at runtime, but nothing pinned its resolution rules. These tests are derived from
 * the module's own logic (the fallback chain in `stateFile`, the mood-over-state
 * priority in `resolve`, the format inference and colorisation in `sourceFor`, and
 * the cache invalidation on `setColorMap` / `setPack`) — they document what the code
 * DOES, not a wished-for behaviour.
 */
import { describe, it, expect } from 'vitest'
import { AnimationResolver, toDataUri } from '../apps/crew-companion/animationResolver'
import { applySvgColorMap } from '../apps/crew-companion/colorCustomizer'
import type { PackManifest } from '../apps/crew-companion/appearanceTypes'
import type { PetState, PetMood } from '../apps/crew-companion/types'

// ── Fixtures ─────────────────────────────────────────────────────────────

const IDLE_SVG = '<svg><rect fill="#ff0000"/></svg>'
const ERROR_SVG = '<svg><circle/></svg>'
const HAPPY_SVG = '<svg id="happy"/>'
const WAVE_SVG = '<svg id="wave"/>'
const LOADING_LOTTIE = '{"v":1,"fr":30}'
const DONE_SPRITE_B64 = 'QUJD' // arbitrary base64-ish payload, no data: prefix
const BUSY_SPRITE_DATAURI = 'data:image/png;base64,PREENCODED'

/** A pack that exercises every format and the interesting fallback branches. */
function makeManifest(): PackManifest {
  return {
    meta: {
      id: 'pack-a',
      name: 'Pack A',
      author: 'tester',
      description: '',
      type: 'built-in',
      format: 'svg',
      thumbnail: 'thumb.svg',
    },
    // Deliberately omit walking/thinking/working/offline/peeking so the fallback
    // chains in stateFile() are reachable through resolve().
    states: {
      idle: 'idle.svg',
      done: 'done.png',
      error: 'error.svg',
      loading: 'loading.json',
    },
    // curious/sleepy absent on purpose → mood falls through to the state file.
    moods: {
      happy: 'happy.svg',
      busy: 'busy.png',
    },
    random: {
      wave: 'wave.svg',
    },
  }
}

function makeContentMap(): Record<string, string> {
  return {
    'idle.svg': IDLE_SVG,
    'error.svg': ERROR_SVG,
    'happy.svg': HAPPY_SVG,
    'wave.svg': WAVE_SVG,
    'loading.json': LOADING_LOTTIE,
    'done.png': DONE_SPRITE_B64,
    'busy.png': BUSY_SPRITE_DATAURI,
  }
}

function makeResolver(): { r: AnimationResolver; content: Record<string, string> } {
  const content = makeContentMap()
  return { r: new AnimationResolver(makeManifest(), content), content }
}

// ── toDataUri ──────────────────────────────────────────────────────────────

describe('toDataUri', () => {
  it('URI-encodes the SVG body behind the svg+xml prefix', () => {
    const raw = '<svg a="b c"/>'
    expect(toDataUri(raw)).toBe(`data:image/svg+xml,${encodeURIComponent(raw)}`)
  })

  it('strips a leading <?xml …?> declaration (and following whitespace) before encoding', () => {
    const body = '<svg/>'
    expect(toDataUri(`<?xml version="1.0" encoding="UTF-8"?>\n${body}`)).toBe(
      `data:image/svg+xml,${encodeURIComponent(body)}`,
    )
  })

  it('turns empty content into the bare svg+xml prefix', () => {
    expect(toDataUri('')).toBe('data:image/svg+xml,')
  })
})

// ── Format inference + source building (via resolve) ─────────────────────────

describe('resolve — format inference', () => {
  it('svg state → svg data URI derived from toDataUri(raw)', () => {
    const { r } = makeResolver()
    const src = r.resolve('idle', 'neutral')
    expect(src.format).toBe('svg')
    expect(src.uri).toBe(toDataUri(IDLE_SVG))
  })

  it('.json state → lottie source returned verbatim (no encoding)', () => {
    const { r } = makeResolver()
    const src = r.resolve('loading' as PetState, 'neutral')
    expect(src.format).toBe('lottie')
    expect(src.uri).toBe(LOADING_LOTTIE)
  })

  it('.png state without a data: prefix → base64 PNG data URI', () => {
    const { r } = makeResolver()
    const src = r.resolve('done' as PetState, 'neutral')
    expect(src.format).toBe('sprite')
    expect(src.uri).toBe(`data:image/png;base64,${DONE_SPRITE_B64}`)
  })

  it('.png content that is already a data: URI is passed through untouched', () => {
    const { r } = makeResolver()
    // busy mood maps to busy.png whose content already starts with data:
    const src = r.resolve('idle', 'busy')
    expect(src.format).toBe('sprite')
    expect(src.uri).toBe(BUSY_SPRITE_DATAURI)
  })
})

// ── Mood-over-state priority ─────────────────────────────────────────────────

describe('resolve — mood priority', () => {
  it('a non-neutral mood WITH pack art overrides the state animation', () => {
    const { r } = makeResolver()
    expect(r.resolve('idle', 'happy').uri).toBe(toDataUri(HAPPY_SVG))
  })

  it('a non-neutral mood WITHOUT pack art falls back to the state animation', () => {
    const { r } = makeResolver()
    // curious not in moods → resolves the idle state file instead.
    expect(r.resolve('idle', 'curious' as PetMood).uri).toBe(toDataUri(IDLE_SVG))
  })

  it('neutral mood always resolves the state animation', () => {
    const { r } = makeResolver()
    expect(r.resolve('error' as PetState, 'neutral').uri).toBe(toDataUri(ERROR_SVG))
  })
})

// ── State fallback chain (stateFile, exercised via resolve) ──────────────────

describe('resolve — state fallback chain', () => {
  // Table of (requested state) → (file it should ultimately resolve to) for the
  // manifest above, which provides only idle/done/error/loading.
  const cases: Array<[string, string]> = [
    ['idle', IDLE_SVG],          // present directly
    ['thinking', LOADING_LOTTIE], // thinking → [loading, idle] → loading present
    ['working', LOADING_LOTTIE],  // working  → [loading, thinking, idle] → loading
    ['walking', IDLE_SVG],        // walking  → [idle]
    ['offline', IDLE_SVG],        // offline  → [idle]
    ['peeking', IDLE_SVG],        // peeking  → [idle]
    ['peekThinking', IDLE_SVG],   // peekThinking → [thinking(absent), idle]
    ['totallyUnknown', IDLE_SVG], // default  → [idle]
  ]

  it.each(cases)('state %s resolves through the chain to the expected file', (state, expectedRaw) => {
    const { r } = makeResolver()
    const src = r.resolve(state as PetState, 'neutral')
    // loading is lottie (verbatim); everything else here is svg (data URI).
    const expectedUri = expectedRaw === LOADING_LOTTIE ? LOADING_LOTTIE : toDataUri(expectedRaw)
    expect(src.uri).toBe(expectedUri)
  })

  it('the deeper loading chain picks working when present and loading is absent', () => {
    // loading → [thinking, working, idle]; here only working is provided.
    const manifest = makeManifest()
    manifest.states = { idle: 'idle.svg', working: 'working.svg' }
    const content = { 'idle.svg': IDLE_SVG, 'working.svg': '<svg id="working"/>' }
    const r = new AnimationResolver(manifest, content)
    expect(r.resolve('loading' as PetState, 'neutral').uri).toBe(toDataUri('<svg id="working"/>'))
  })

  it('falls back to an empty svg data URI when even idle is missing', () => {
    // stateFile returns s['idle'] || '' → '' → sourceFor('') → svg of empty content.
    const manifest = makeManifest()
    // No idle at all (structurally invalid, but the resolver must not throw).
    manifest.states = { done: 'done.svg' } as PackManifest['states']
    const r = new AnimationResolver(manifest, {})
    const src = r.resolve('walking' as PetState, 'neutral')
    expect(src.format).toBe('svg')
    expect(src.uri).toBe('data:image/svg+xml,')
  })

  it('missing content for a mapped file resolves to the empty svg body', () => {
    const manifest = makeManifest()
    // idle points at a file that isn't in the content map.
    manifest.states = { idle: 'ghost.svg' }
    const r = new AnimationResolver(manifest, {})
    expect(r.resolve('idle', 'neutral').uri).toBe(toDataUri(''))
  })
})

// ── Colorisation ─────────────────────────────────────────────────────────────

describe('resolve — colour map', () => {
  it('applies the colour map to svg content before encoding', () => {
    const { r } = makeResolver()
    const map = { '#ff0000': '#00ff00' }
    r.setColorMap(map)
    const expected = toDataUri(applySvgColorMap(IDLE_SVG, map))
    expect(r.resolve('idle', 'neutral').uri).toBe(expected)
    // Confidence check: colourisation actually changed the bytes.
    expect(r.resolve('idle', 'neutral').uri).not.toBe(toDataUri(IDLE_SVG))
  })

  it('leaves non-svg (sprite/lottie) sources untouched by the colour map', () => {
    const { r } = makeResolver()
    r.setColorMap({ '#ff0000': '#00ff00' })
    expect(r.resolve('loading' as PetState, 'neutral').uri).toBe(LOADING_LOTTIE)
    expect(r.resolve('done' as PetState, 'neutral').uri).toBe(`data:image/png;base64,${DONE_SPRITE_B64}`)
  })
})

// ── Caching + invalidation ───────────────────────────────────────────────────

describe('caching', () => {
  it('returns a cached source for a repeated (state, mood) key even if content changes underneath', () => {
    const { r, content } = makeResolver()
    const first = r.resolve('idle', 'neutral')
    content['idle.svg'] = '<svg>MUTATED</svg>' // mutate the underlying map
    const second = r.resolve('idle', 'neutral')
    expect(second.uri).toBe(first.uri) // cache hit — mutation not observed
  })

  it('setColorMap clears the cache so the next resolve re-reads content', () => {
    const { r, content } = makeResolver()
    r.resolve('idle', 'neutral')
    content['idle.svg'] = '<svg>MUTATED</svg>'
    r.setColorMap(null) // clears cache (even when clearing to null)
    expect(r.resolve('idle', 'neutral').uri).toBe(toDataUri('<svg>MUTATED</svg>'))
  })

  it('setPack swaps the manifest/content and clears the cache', () => {
    const { r } = makeResolver()
    r.resolve('idle', 'neutral') // populate cache under pack-a
    const otherManifest = makeManifest()
    otherManifest.meta.id = 'pack-b'
    otherManifest.states = { idle: 'idle.svg' }
    r.setPack(otherManifest, { 'idle.svg': '<svg id="b"/>' })
    expect(r.resolve('idle', 'neutral').uri).toBe(toDataUri('<svg id="b"/>'))
  })
})

// ── Introspection helpers ────────────────────────────────────────────────────

describe('hasState / hasMood', () => {
  it('hasState is true only for states the pack actually declares', () => {
    const { r } = makeResolver()
    expect(r.hasState('idle')).toBe(true)
    expect(r.hasState('done')).toBe(true)
    expect(r.hasState('walking')).toBe(false) // fallback-only, not declared
  })

  it('hasMood is true only for moods with pack art', () => {
    const { r } = makeResolver()
    expect(r.hasMood('happy')).toBe(true)
    expect(r.hasMood('busy')).toBe(true)
    expect(r.hasMood('curious')).toBe(false)
  })
})

describe('random clips', () => {
  it('randomNames lists the pack random keys', () => {
    const { r } = makeResolver()
    expect(r.randomNames()).toEqual(['wave'])
  })

  it('randomNames is empty when the pack has no random block', () => {
    const manifest = makeManifest()
    delete manifest.random
    const r = new AnimationResolver(manifest, makeContentMap())
    expect(r.randomNames()).toEqual([])
  })

  it('resolveRandom returns a source for a known clip and null for an unknown one', () => {
    const { r } = makeResolver()
    const wave = r.resolveRandom('wave')
    expect(wave).not.toBeNull()
    expect(wave!.uri).toBe(toDataUri(WAVE_SVG))
    expect(wave!.format).toBe('svg')
    expect(r.resolveRandom('nope')).toBeNull()
  })

  it('resolveRandom returns null when the pack has no random block at all', () => {
    const manifest = makeManifest()
    delete manifest.random
    const r = new AnimationResolver(manifest, makeContentMap())
    expect(r.resolveRandom('wave')).toBeNull()
  })
})
