// What the pet bridge sends over the wire, and how it reads back.
//
// The bridge is the only thing between the editor's authoring vocabulary (states /
// moods / random) and the store's manifest. The regression these tests lock in: a
// pack saved by the editor MUST keep its three categories as three manifest maps.
// Folding moods and random into `states` saved without error but read back
// indistinguishable — pack_detail could no longer tell which slots were random, so
// `randomNames` came back empty and the next edit dropped those clips off disk. The
// backend already reads three categories and persists whatever manifest it is given;
// the fix and these tests live entirely on the send side.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { petBridge } from '../apps/crew-companion/petBridge'
import type { PackInput } from '../apps/crew-companion/petBridge'

interface SavePayload {
  id: string
  manifest: {
    meta: Record<string, unknown>
    states?: Record<string, string>
    moods?: Record<string, string>
    random?: Record<string, string>
  }
  files: Record<string, string>
}

/**
 * A fetch double that records every request body and answers with a caller-chosen
 * status + JSON. The bridge's helpers read `r.ok` (postJson) or `r.json()`
 * (postForJson / getJson), so both shapes are covered by returning a real-ish object.
 */
function stubFetch(reply: { ok?: boolean; body?: unknown } = {}) {
  const calls: Array<{ url: string; body: unknown }> = []
  const fn = vi.fn(async (url: string, init?: RequestInit) => {
    const raw = init?.body
    calls.push({ url, body: typeof raw === 'string' ? JSON.parse(raw) : undefined })
    return {
      ok: reply.ok ?? true,
      json: async () => reply.body ?? { ok: true },
    } as unknown as Response
  })
  vi.stubGlobal('fetch', fn)
  return { fn, calls }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('gallerySavePack — category preservation', () => {
  it('keeps states, moods and random in their OWN manifest maps', async () => {
    const { fn, calls } = stubFetch({ body: { ok: true } })
    const data: PackInput = {
      meta: { id: 'my-pack', name: 'My Pack' },
      states: { idle: '<svg>idle</svg>' },
      moods: { happy: '<svg>happy</svg>' },
      random: { wiggle: '<svg>wiggle</svg>' },
    }

    const result = await petBridge.gallerySavePack!(data)

    expect(result).toEqual({ ok: true })
    expect(fn).toHaveBeenCalledOnce()
    const payload = calls[0].body as SavePayload
    const { manifest, files } = payload

    // Each category is its own map — this is the whole point of the fix.
    expect(Object.keys(manifest.states ?? {})).toEqual(['idle'])
    expect(Object.keys(manifest.moods ?? {})).toEqual(['happy'])
    expect(Object.keys(manifest.random ?? {})).toEqual(['wiggle'])

    // A manifest map names a slot -> its FILENAME; the content travels in `files`.
    expect(files[manifest.states!.idle]).toBe('<svg>idle</svg>')
    expect(files[manifest.moods!.happy]).toBe('<svg>happy</svg>')
    expect(files[manifest.random!.wiggle]).toBe('<svg>wiggle</svg>')

    // A moods/random slot must NOT leak into states, or pack_detail mislabels it.
    expect(manifest.states).not.toHaveProperty('happy')
    expect(manifest.states).not.toHaveProperty('wiggle')
  })

  it('preserves an author-named random clip as the manifest key while filing it under a safe indexed name', async () => {
    const { calls } = stubFetch({ body: { ok: true } })
    const data: PackInput = {
      meta: { id: 'p', name: 'P' },
      states: { idle: '<svg/>' },
      // A name with spaces/punctuation is fine as a manifest KEY (that is what
      // pack_detail hands back as randomNames) but must not be trusted as a filename.
      random: { 'Look Around!': '<svg>look</svg>' },
    }

    await petBridge.gallerySavePack!(data)
    const { manifest, files } = calls[0].body as SavePayload

    // The real name survives for the editor to restore on re-edit…
    expect(manifest.random!['Look Around!']).toBeDefined()
    // …but the FILENAME is an indexed, path-safe segment, not the raw name.
    const file = manifest.random!['Look Around!']
    expect(file).toBe('random-0.svg')
    expect(files[file]).toBe('<svg>look</svg>')
  })

  it('skips empty and whitespace-only slots instead of writing empty files', async () => {
    const { calls } = stubFetch({ body: { ok: true } })
    const data: PackInput = {
      meta: { id: 'p', name: 'P' },
      states: { idle: '<svg/>', done: '', error: '   ' },
      moods: { happy: '' },
      random: { blank: '  ' },
    }

    await petBridge.gallerySavePack!(data)
    const { manifest, files } = calls[0].body as SavePayload

    expect(Object.keys(manifest.states ?? {})).toEqual(['idle'])
    // Nothing empty was written, and empty categories are omitted entirely.
    expect(manifest.moods).toBeUndefined()
    expect(manifest.random).toBeUndefined()
    expect(Object.keys(files)).toEqual(['idle.svg'])
  })

  it('names lottie (JSON) art .json and everything else .svg, and marks the pack format', async () => {
    const { calls } = stubFetch({ body: { ok: true } })
    const data: PackInput = {
      meta: { id: 'p', name: 'P' },
      states: { idle: '  {"v":"5.7"}  ' }, // leading space then JSON => lottie
      moods: { happy: '<svg>markup</svg>' },
    }

    await petBridge.gallerySavePack!(data)
    const { manifest, files } = calls[0].body as SavePayload

    expect(manifest.states!.idle).toBe('idle.json')
    expect(manifest.moods!.happy).toBe('happy.svg')
    // A single JSON slot is enough to make the whole pack lottie.
    expect(manifest.meta.format).toBe('lottie')
    expect(files['idle.json']).toContain('"v":"5.7"')
  })

  it('defaults format to svg when no slot is JSON', async () => {
    const { calls } = stubFetch({ body: { ok: true } })
    await petBridge.gallerySavePack!({
      meta: { id: 'p', name: 'P' },
      states: { idle: '<svg/>' },
    })
    const { manifest } = calls[0].body as SavePayload
    expect(manifest.meta.format).toBe('svg')
  })

  it('produces the exact { meta, states } manifest for a states-only pack (backward compatible)', async () => {
    const { calls } = stubFetch({ body: { ok: true } })
    await petBridge.gallerySavePack!({
      meta: { id: 'p', name: 'P' },
      states: { idle: '<svg/>' },
    })
    const { manifest } = calls[0].body as SavePayload
    // A pack with no moods/random reads back the same as it always did — pack_detail
    // treats absent categories as empty, so old and new manifests both load.
    expect(Object.keys(manifest).sort()).toEqual(['meta', 'states'])
  })

  it('refuses a pack with no art and does not hit the network', async () => {
    const { fn } = stubFetch({ body: { ok: true } })
    const result = await petBridge.gallerySavePack!({
      meta: { id: 'p', name: 'P' },
      states: { idle: '' },
      moods: { happy: '  ' },
    })
    expect(result).toEqual({ ok: false, error: 'That pack has no art in it' })
    expect(fn).not.toHaveBeenCalled()
  })

  it('refuses a pack with no id before touching the network', async () => {
    const { fn } = stubFetch({ body: { ok: true } })
    const result = await petBridge.gallerySavePack!({
      meta: {},
      states: { idle: '<svg/>' },
    })
    expect(result).toEqual({ ok: false, error: 'Could not save: the pack has no internal id' })
    expect(fn).not.toHaveBeenCalled()
  })
})

describe('petBridge read helpers', () => {
  it('galleryListPacks returns the packs array, and [] when the body is not an array', async () => {
    stubFetch({ body: { packs: [{ id: 'a' }, { id: 'b' }] } })
    expect(await petBridge.galleryListPacks!()).toHaveLength(2)

    vi.unstubAllGlobals()
    stubFetch({ body: { packs: 'nope' } })
    expect(await petBridge.galleryListPacks!()).toEqual([])
  })

  it('remindersList returns the reminders array, and [] when absent', async () => {
    stubFetch({ body: { reminders: [{ id: 'r1' }] } })
    expect(await petBridge.remindersList!()).toEqual([{ id: 'r1' }])

    vi.unstubAllGlobals()
    stubFetch({ body: {} })
    expect(await petBridge.remindersList!()).toEqual([])
  })

  it('galleryDeletePack reports the HTTP outcome as a boolean', async () => {
    const { calls } = stubFetch({ ok: true, body: { ok: true } })
    expect(await petBridge.galleryDeletePack!('some-pack')).toBe(true)
    expect(calls[0].body).toMatchObject({ id: 'some-pack' })

    vi.unstubAllGlobals()
    stubFetch({ ok: false })
    expect(await petBridge.galleryDeletePack!('some-pack')).toBe(false)
  })
})

describe('cross-window appearance broadcast', () => {
  // The pet overlay is a SEPARATE window: in-page listener fan-out cannot reach
  // it, only the main-process broadcast (preload().appearanceChanged) spans
  // both. The regression: choosing an accessory or a recolour persisted to disk
  // but the live companion stayed visually unchanged until reload, because only
  // gallerySetActive ever broadcast.
  function stubPreload() {
    const appearanceChanged = vi.fn()
    ;(window as unknown as { crewCompanion?: unknown }).crewCompanion = { appearanceChanged }
    return appearanceChanged
  }

  afterEach(() => {
    delete (window as unknown as { crewCompanion?: unknown }).crewCompanion
  })

  it('a recolour save broadcasts to the overlay window', async () => {
    stubFetch({ ok: true })
    const broadcast = stubPreload()
    await petBridge.gallerySetColorMap!('kiro-ghost', { '#fff': '#f0f' })
    expect(broadcast).toHaveBeenCalledOnce()
  })

  it('a FAILED recolour does not broadcast', async () => {
    stubFetch({ ok: false })
    const broadcast = stubPreload()
    await petBridge.gallerySetColorMap!('kiro-ghost', { '#fff': '#f0f' })
    expect(broadcast).not.toHaveBeenCalled()
  })

  it('an accessory config save broadcasts to the overlay window', async () => {
    stubFetch({ body: { ok: true } })
    const broadcast = stubPreload()
    await petBridge.updateConfig!({ kiro: { accessory: 'scarf' } })
    expect(broadcast).toHaveBeenCalledOnce()
  })

  it('an unrelated config save does NOT repaint the overlay', async () => {
    stubFetch({ body: { ok: true } })
    const broadcast = stubPreload()
    await petBridge.updateConfig!({ sessionNotifications: false })
    expect(broadcast).not.toHaveBeenCalled()
  })
})

describe('non-2xx responses are refusals, not successes', () => {
  // The gateway's error bodies ({error, code}) carry no `ok` field. Returning
  // them verbatim from postForJson made callers that check `result.ok ===
  // false` treat a FAILED save as success — the editor closed and the user's
  // unsaved artwork was discarded. postForJson now stamps ok:false on any
  // non-2xx body.
  it('a 503 save reports ok:false to the editor, keeping the reason', async () => {
    stubFetch({ ok: false, body: { error: 'crew-companion could not save to disk', code: 'store_write_failed' } })
    const result = await petBridge.gallerySavePack!({
      meta: { id: 'p', name: 'P' },
      states: { idle: '<svg/>' },
    })
    expect(result && (result as { ok?: boolean }).ok).toBe(false)
    expect((result as { error?: string }).error).toContain('could not save')
  })

  it('a failed config save returns false and notifies nobody', async () => {
    stubFetch({ ok: false, body: { error: 'store_write_failed' } })
    const heard = vi.fn()
    const off = petBridge.onConfigUpdated!(heard)
    const saved = await petBridge.updateConfig!({ kiro: { accessory: 'scarf' } })
    off()
    expect(saved).toBe(false)
    expect(heard).not.toHaveBeenCalled()
  })
})

describe('sprite-pack random clip filenames never collide', () => {
  // Sanitization is lossy: "look-away" and "look away" both collapse to
  // "look_away", so two distinct clips mapped to ONE file and one clip's
  // artwork silently overwrote the other's. Filenames now carry an index.
  it('two names that sanitize identically get distinct files', async () => {
    const { calls } = stubFetch({ body: { ok: true } })
    await petBridge.gallerySaveSpritePack!({
      name: 'P', author: '', description: '',
      frameWidth: 32, frameHeight: 32, fps: 8, flipX: false, offsetY: 0,
      assignments: { idle: 'data:image/png;base64,QQ==' },
      randomAssignments: {
        'look-away': 'data:image/png;base64,QQ==',
        'look away': 'data:image/png;base64,Qg==',
      },
      rowAssignments: {},
    })
    const payload = calls[0].body as {
      manifest: { random: Record<string, string> }
      files: Record<string, string>
    }
    const filenames = Object.values(payload.manifest.random)
    expect(new Set(filenames).size).toBe(2)
    // Each clip's own art survives under its own file.
    expect(payload.files[payload.manifest.random['look-away']]).toBe('QQ==')
    expect(payload.files[payload.manifest.random['look away']]).toBe('Qg==')
  })
})
