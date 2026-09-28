import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

/**
 * Every bundled SVG asset must be well-formed XML.
 *
 * This exists because a malformed SVG fails SILENTLY: the browser refuses to
 * parse it, the `<img>` renders nothing, and the only symptom is a missing icon
 * that no other gate looks at. It cost a round on the Feishu mark — a provenance
 * comment containing a double hyphen, which XML forbids inside a comment, shipped
 * a blank channel icon that a diff review could not have caught.
 *
 * ## Why there are two checks and not one
 *
 * They are not redundant, and the double-hyphen one is not the weaker of the
 * pair. Revert-verifying against the real mistake shows jsdom's `DOMParser`
 * ACCEPTS `--` inside a comment while Chromium rejects the whole document, so the
 * parse case does not fire on it and the explicit string check is the only thing
 * standing between that mistake and a blank icon. The parse case earns its place
 * on every other malformation — an unclosed tag, a stray `&`, a bad attribute —
 * where jsdom and the browser agree.
 *
 * jsdom's `DOMParser` also does not throw on malformed XML; it returns a document
 * whose root is `parsererror`, which is what the browser does too, so that is the
 * condition to check.
 */
const ASSETS = join(__dirname, '..', 'assets')

const svgFiles = readdirSync(ASSETS).filter(f => f.endsWith('.svg')).sort()

describe('bundled SVG assets', () => {
  it('finds SVG files to check', () => {
    // A rename or move of the assets dir would otherwise make every case below
    // vacuously pass, which is the failure mode this whole file guards against.
    expect(svgFiles.length).toBeGreaterThanOrEqual(8)
  })

  it.each(svgFiles)('%s parses as XML', file => {
    const text = readFileSync(join(ASSETS, file), 'utf-8')
    const doc = new DOMParser().parseFromString(text, 'image/svg+xml')
    const err = doc.querySelector('parsererror')
    expect(err?.textContent ?? '').toBe('')
    expect(doc.documentElement.tagName.toLowerCase()).toBe('svg')
  })

  it.each(svgFiles)('%s has no double hyphen inside a comment', file => {
    const text = readFileSync(join(ASSETS, file), 'utf-8')
    for (const [, body] of text.matchAll(/<!--([\s\S]*?)-->/g)) {
      expect(body).not.toContain('--')
    }
  })
})
