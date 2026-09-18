/**
 * Which Lottie documents are refused for asking the player to FETCH something.
 *
 * A pack's clip is third-party art rendered on the gateway's own authenticated
 * origin, and `lottie-web` resolves a document's `assets` and `fonts` by
 * requesting them — so a clip carrying an external image makes the dashboard
 * issue an attacker-chosen request the moment a crew wears that pack. The
 * predicate is conservative on purpose: anything it cannot prove is inline
 * counts as remote, so a document shape nobody anticipated is refused rather
 * than fetched.
 */
import { describe, expect, it } from 'vitest'

import { referencesRemoteAsset } from '../lib/appearancePacks/lottieSafety'

/** A minimal document, with whatever the test is about spliced in. */
const doc = (over: Record<string, unknown> = {}) => ({
  v: '5.7.4',
  fr: 24,
  ip: 0,
  op: 48,
  layers: [],
  ...over,
})

describe('a clip that fetches nothing', () => {
  it('allows a pure vector document', () => {
    // What the Companion's own editor produces: shapes only, no assets array.
    expect(referencesRemoteAsset(doc())).toBe(false)
  })

  it('allows an empty assets array', () => {
    expect(referencesRemoteAsset(doc({ assets: [] }))).toBe(false)
  })

  it('allows a genuinely embedded image', () => {
    expect(
      referencesRemoteAsset(
        doc({ assets: [{ id: 'i0', w: 8, h: 8, e: 1, p: 'data:image/png;base64,iVBORw0=' }] }),
      ),
    ).toBe(false)
  })

  it('allows a precomp asset, which is nested layers rather than a file', () => {
    expect(referencesRemoteAsset(doc({ assets: [{ id: 'comp_0', layers: [{ ty: 4 }] }] }))).toBe(false)
  })

  it('allows a font the document only NAMES', () => {
    expect(
      referencesRemoteAsset(doc({ fonts: { list: [{ fFamily: 'Arial', fName: 'Arial', origin: 0 }] } })),
    ).toBe(false)
  })
})

describe('a clip that would issue a request', () => {
  it('refuses an external image asset', () => {
    // The finding's own trigger: `u` is a directory prefix that exists only to be
    // joined onto `p` and requested.
    expect(
      referencesRemoteAsset(
        doc({ assets: [{ id: 'i0', w: 1, h: 1, u: 'https://attacker.example/', p: 'pixel.png', e: 0 }] }),
      ),
    ).toBe(true)
  })

  it('refuses a relative path asset', () => {
    expect(referencesRemoteAsset(doc({ assets: [{ id: 'i0', u: 'images/', p: 'img_0.png' }] }))).toBe(true)
  })

  it('refuses an asset that claims to be embedded but carries a path', () => {
    // `e: 1` is the author's claim; the `p` is what the player actually resolves.
    expect(referencesRemoteAsset(doc({ assets: [{ id: 'i0', e: 1, p: 'img_0.png' }] }))).toBe(true)
  })

  it('refuses an asset with no embedded marker at all', () => {
    // Conservative: unprovable counts as remote, so an unanticipated shape is
    // refused rather than fetched.
    expect(referencesRemoteAsset(doc({ assets: [{ id: 'i0', p: 'data:image/png;base64,iVBORw0=' }] }))).toBe(true)
  })

  it('refuses a `u` prefix even when `p` is a data URI', () => {
    expect(
      referencesRemoteAsset(
        doc({ assets: [{ id: 'i0', e: 1, u: 'https://attacker.example/', p: 'data:image/png;base64,iVBORw0=' }] }),
      ),
    ).toBe(true)
  })

  it('refuses a webfont by path', () => {
    expect(referencesRemoteAsset(doc({ fonts: { list: [{ fFamily: 'X', fPath: 'https://attacker.example/f.woff' }] } })))
      .toBe(true)
  })

  it('refuses a webfont by non-local origin', () => {
    expect(referencesRemoteAsset(doc({ fonts: { list: [{ fFamily: 'X', origin: 3 }] } }))).toBe(true)
  })

  it('refuses when only ONE of several assets is remote', () => {
    expect(
      referencesRemoteAsset(
        doc({
          assets: [
            { id: 'i0', e: 1, p: 'data:image/png;base64,iVBORw0=' },
            { id: 'comp_0', layers: [] },
            { id: 'i1', u: 'https://attacker.example/', p: 'beacon.png' },
          ],
        }),
      ),
    ).toBe(true)
  })
})

describe('junk', () => {
  it('answers false for anything that is not a document', () => {
    // Total: an unparseable body has no assets, and `LottieRenderer` refuses it
    // on its own for being unparseable.
    for (const junk of [null, undefined, 42, 'nope', [], { assets: 'nope' }, { fonts: 'nope' }]) {
      expect(referencesRemoteAsset(junk)).toBe(false)
    }
  })

  it('ignores a non-object entry inside assets', () => {
    expect(referencesRemoteAsset(doc({ assets: [null, 'nope', 7] }))).toBe(false)
  })
})
