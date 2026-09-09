/**
 * `lib/appearancePacks/library` — the pack library's pure half.
 *
 * These four functions are what the picker and the renderer AGREE on: where a
 * slot's art is, which packs a crew can wear, what a listing row means, and
 * whether a picked file is a bundle at all. A drift between the picker and the
 * face it picks shows up here first, which is why they are pure and tested apart
 * from any component.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, it, expect } from 'vitest'
import {
  BUILTIN_PACK_ID,
  MAX_BUNDLE_BYTES,
  PACK_BUNDLE_KIND,
  bundleFromText,
  isWearableFormat,
  packSlotUrl,
  packSummariesFrom,
} from '../lib/appearancePacks/library'

describe('packSlotUrl', () => {
  it('addresses one slot on the crew appearance route', () => {
    expect(packSlotUrl('aurora', 'idle')).toBe('/api/appearances/aurora/slot/idle')
    expect(packSlotUrl('aurora', 'working')).toBe('/api/appearances/aurora/slot/working')
  })

  it('encodes both segments, so a hostile id cannot climb the path', () => {
    // The backend refuses such an id anyway; encoding here means the request
    // never reaches a DIFFERENT route in the first place.
    expect(packSlotUrl('../../etc/passwd', 'idle')).toBe(
      '/api/appearances/..%2F..%2Fetc%2Fpasswd/slot/idle',
    )
    expect(packSlotUrl('aurora', 'a/b')).toBe('/api/appearances/aurora/slot/a%2Fb')
  })
})

describe('isWearableFormat', () => {
  it('accepts SVG only — the face is an <img> and core ships no player', () => {
    expect(isWearableFormat('svg')).toBe(true)
    expect(isWearableFormat('lottie')).toBe(false)
    expect(isWearableFormat('sprite')).toBe(false)
    expect(isWearableFormat('')).toBe(false)
  })
})

describe('packSummariesFrom', () => {
  it('reads the rows of a well-formed listing', () => {
    expect(
      packSummariesFrom({
        packs: [
          {
            id: BUILTIN_PACK_ID,
            name: 'Kiro',
            author: 'Kiro Crew',
            description: 'The default companion.',
            type: 'builtin',
            format: 'svg',
            recoloured: false,
          },
          {
            id: 'aurora',
            name: 'Aurora',
            author: 'zoe',
            description: 'a paper fox',
            type: 'custom',
            format: 'lottie',
            recoloured: true,
          },
        ],
      }),
      // `description` and `recoloured` are served but nothing renders them, so
      // the summary does not carry them: a field arrives with its surface.
    ).toEqual([
      {
        id: BUILTIN_PACK_ID,
        name: 'Kiro',
        author: 'Kiro Crew',
        type: 'builtin',
        format: 'svg',
      },
      {
        id: 'aurora',
        name: 'Aurora',
        author: 'zoe',
        type: 'custom',
        format: 'lottie',
      },
    ])
  })

  it('is total: a junk payload is an empty library, not a crash', () => {
    expect(packSummariesFrom(undefined)).toEqual([])
    expect(packSummariesFrom(null)).toEqual([])
    expect(packSummariesFrom('packs')).toEqual([])
    expect(packSummariesFrom({})).toEqual([])
    expect(packSummariesFrom({ packs: 'nope' })).toEqual([])
    expect(packSummariesFrom({ packs: [null, 7, 'x'] })).toEqual([])
  })

  it('drops a row with no usable id — it could not be selected, fetched or deleted', () => {
    expect(packSummariesFrom({ packs: [{ name: 'nameless' }, { id: '', name: 'blank' }] })).toEqual([])
  })

  it('names a row by its id when the metadata is missing, and defaults forgivingly', () => {
    expect(packSummariesFrom({ packs: [{ id: 'aurora' }] })).toEqual([
      {
        id: 'aurora',
        name: 'aurora',
        author: '',
        // Anything but the built-in marker reads as custom: the only thing
        // `type` gates is the delete control, which the server re-checks.
        type: 'custom',
        // An unknown format reads as SVG, which is the only wearable one — a
        // row that claimed nothing must not be silently unselectable.
        format: 'svg',
      },
    ])
  })

  it('keeps a declared non-SVG format, so the picker can grey that row', () => {
    const [lottie, sprite] = packSummariesFrom({
      packs: [{ id: 'a', format: 'lottie' }, { id: 'b', format: 'sprite' }],
    })
    expect(lottie.format).toBe('lottie')
    expect(sprite.format).toBe('sprite')
  })
})

describe('bundleFromText', () => {
  // The shape `appearance_packs/transfer.py::export_bundle` actually emits — envelope
  // included. A fixture written from what this module BELIEVED the format was is how
  // the import path shipped broken: it type-checked, it passed here, and the server
  // refused every real export.
  const bundle = JSON.stringify({
    kind: 'crew-companion-pack',
    version: 1,
    id: 'aurora',
    manifest: { meta: { id: 'aurora', format: 'svg' }, states: { idle: 'idle.svg' } },
    files: { 'idle.svg': '<svg/>' },
  })

  it('accepts a real exported bundle', () => {
    const result = bundleFromText(bundle)
    expect(result.ok).toBe(true)
    if (!result.ok) return
    expect(result.bundle.files['idle.svg']).toBe('<svg/>')
  })

  it('carries the ENVELOPE through, because the importer rules on it', () => {
    // `import_bundle` refuses a payload whose `kind` is not PACK_BUNDLE_KIND and
    // takes the pack id from `id`, so dropping either makes every import fail with
    // a message this client wrote about a bundle that was valid.
    const result = bundleFromText(bundle)
    expect(result.ok).toBe(true)
    if (!result.ok) return
    expect(result.bundle.kind).toBe(PACK_BUNDLE_KIND)
    expect(result.bundle.version).toBe(1)
    expect(result.bundle.id).toBe('aurora')
  })

  it('passes an unknown envelope field through rather than dropping it', () => {
    // A field a newer exporter adds is the server's business, not this reader's.
    const result = bundleFromText(
      JSON.stringify({ kind: PACK_BUNDLE_KIND, id: 'aurora', signature: 'abc', manifest: {}, files: {} }),
    )
    expect(result.ok).toBe(true)
    if (!result.ok) return
    expect(result.bundle.signature).toBe('abc')
  })

  it('refuses a manifest + files document with no envelope', () => {
    // Valid-looking and NOT a pack file: the importer would refuse it, and saying
    // so before the upload beats the same sentence a round-trip later.
    expect(
      bundleFromText(JSON.stringify({ manifest: {}, files: { 'idle.svg': '<svg/>' } })),
    ).toEqual({ ok: false, reason: 'unreadable' })
    expect(bundleFromText(JSON.stringify({ kind: 'something-else', manifest: {}, files: {} }))).toEqual(
      { ok: false, reason: 'unreadable' },
    )
  })

  it('pins PACK_BUNDLE_KIND to the importer that checks it', () => {
    // A cross-LANGUAGE contract, so no type can hold it: if the Python literal
    // moves and this constant does not, every import breaks again exactly as it
    // did before, and silently.
    const transfer = readFileSync(
      resolve(__dirname, '../../../src/kiro_crew/appearance_packs/transfer.py'),
      'utf8',
    )
    expect(transfer).toContain(`"kind": "${PACK_BUNDLE_KIND}"`)
    expect(transfer).toContain(`payload.get("kind") != "${PACK_BUNDLE_KIND}"`)
  })

  it('refuses what is not JSON at all', () => {
    expect(bundleFromText('not json')).toEqual({ ok: false, reason: 'unreadable' })
    expect(bundleFromText('')).toEqual({ ok: false, reason: 'unreadable' })
  })

  it('refuses a JSON document that is not a bundle', () => {
    const env = `"kind":"${PACK_BUNDLE_KIND}","id":"aurora"`
    expect(bundleFromText('[]')).toEqual({ ok: false, reason: 'unreadable' })
    expect(bundleFromText('"aurora"')).toEqual({ ok: false, reason: 'unreadable' })
    expect(bundleFromText('{}')).toEqual({ ok: false, reason: 'unreadable' })
    // Each of these carries a valid envelope, so it is the manifest/files half
    // being judged rather than the `kind` check short-circuiting the case.
    expect(bundleFromText(`{${env},"manifest":{}}`)).toEqual({ ok: false, reason: 'unreadable' })
    expect(bundleFromText(`{${env},"files":{}}`)).toEqual({ ok: false, reason: 'unreadable' })
    // A list where a map belongs: `files` is a name → content map.
    expect(bundleFromText(`{${env},"manifest":{},"files":[]}`)).toEqual({ ok: false, reason: 'unreadable' })
    expect(bundleFromText(`{${env},"manifest":[],"files":{}}`)).toEqual({ ok: false, reason: 'unreadable' })
  })

  it('refuses an over-large bundle without parsing it', () => {
    // Length is checked FIRST: parsing a 24 MB document in order to then reject
    // it is the cost this bound exists to avoid.
    const huge = `{"kind":"${PACK_BUNDLE_KIND}","manifest":{},"files":{}}`.padEnd(
      MAX_BUNDLE_BYTES + 1,
      ' ',
    )
    expect(bundleFromText(huge)).toEqual({ ok: false, reason: 'too_large' })
  })
})
