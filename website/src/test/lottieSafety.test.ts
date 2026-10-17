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
 *
 * A remote font entry is refused for a second reason: for one of its origins the
 * player builds an `@font-face` rule out of the entry's own `fFamily` text and
 * appends a `<style>` to the inline SVG, which is a stylesheet for the whole
 * dashboard document. The entry never reaching the player is what keeps that
 * text out of the DOM.
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

/** A document whose single font entry is the one under test. */
const withFont = (entry: Record<string, unknown>) => doc({ fonts: { list: [entry] } })

/** `fFamily` text that closes the player's own `@font-face` rule and opens a
 *  rule of its own — what a refused entry keeps out of the document. */
const CSS_BREAKOUT = '") } </style><style>body{display:none}'

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

  it('allows every shape of local font entry the player recognises', () => {
    // The player's own local set: `'n'`, the empty string, numeric 0, and the
    // key being absent. A system family named by one of those fetches nothing
    // and builds no rule.
    for (const local of [
      { fFamily: 'Arial' },
      { fFamily: 'Arial', fOrigin: 'n' },
      { fFamily: 'Arial', fOrigin: '' },
      { fFamily: 'Arial', fOrigin: 'n', origin: 0 },
      { fFamily: 'Arial', fPath: '' },
      { fFamily: 'Arial', origin: 0 },
    ]) {
      expect(referencesRemoteAsset(withFont(local))).toBe(false)
    }
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

  it('refuses each numeric origin that names a remote service', () => {
    for (const origin of [1, 2, 3]) {
      expect(referencesRemoteAsset(withFont({ fFamily: 'X', origin }))).toBe(true)
    }
  })

  it('refuses a non-string `fPath` that the player still reads as a path', () => {
    // The player's local branch is `if (!fPath)`, so an object, an array, a
    // number or `true` all skip it and reach a branch that fetches or writes a
    // rule. A predicate that asked for a string would call each of these local.
    for (const fPath of [{}, ['x'], 1, true, { toString: () => 'x' }]) {
      expect(referencesRemoteAsset(withFont({ fFamily: 'X', fPath }))).toBe(true)
    }
  })

  it('refuses the entry that would write attacker CSS into the dashboard document', () => {
    // `fOrigin: 'p'` plus a truthy `fPath` is the branch that appends a
    // `<style>` built from `fFamily`. The SVG is inline, so the rule applies to
    // the whole page rather than to the clip.
    expect(referencesRemoteAsset(withFont({ fFamily: CSS_BREAKOUT, fOrigin: 'p', fPath: {} }))).toBe(true)
  })

  it('refuses each `fOrigin` string code that names a remote service', () => {
    // `'p'` Google, `'g'` a URL, `'t'` Typekit — remote whatever `fPath` holds,
    // including an empty one.
    expect(referencesRemoteAsset(withFont({ fFamily: 'X', fOrigin: 'g', fPath: '' }))).toBe(true)
    expect(referencesRemoteAsset(withFont({ fFamily: 'X', fOrigin: 't' }))).toBe(true)
    expect(referencesRemoteAsset(withFont({ fFamily: 'X', fOrigin: 'p' }))).toBe(true)
  })

  it('refuses an origin value it cannot place', () => {
    // Fail closed: a code this module does not know, and a value of a type the
    // key is not supposed to hold, are both unprovable rather than local.
    for (const entry of [
      { fOrigin: 'z' },
      { fOrigin: 3 },
      { fOrigin: {} },
      { fOrigin: [] },
      { fOrigin: true },
      { origin: 'p' },
      { origin: {} },
      { origin: true },
    ]) {
      expect(referencesRemoteAsset(withFont({ fFamily: 'X', ...entry }))).toBe(true)
    }
  })

  it('refuses when only ONE of several fonts is remote', () => {
    expect(
      referencesRemoteAsset(
        doc({
          fonts: {
            list: [
              { fFamily: 'Arial', fOrigin: 'n' },
              { fFamily: 'X', fOrigin: 'p', fPath: ['x'] },
            ],
          },
        }),
      ),
    ).toBe(true)
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

  it('ignores a non-object entry inside the font list', () => {
    expect(referencesRemoteAsset(doc({ fonts: { list: [null, 'nope', 7] } }))).toBe(false)
  })
})
