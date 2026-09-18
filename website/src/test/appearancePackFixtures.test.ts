/**
 * The three sample bundles that make the pack tier testable BY HAND.
 *
 * They exist so a reviewer can import one per format from the Library tab and
 * watch a crew wear it (see the PR's manual-verification steps). That only works
 * while each is a bundle the importer accepts and art the player can read, and
 * neither property survives an edit on its own — so both are pinned here rather
 * than left to be discovered by a failed manual pass.
 */
import { readFileSync } from 'node:fs'
import { inflateSync } from 'node:zlib'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

import { bundleFromText, PACK_BUNDLE_KIND } from '../lib/appearancePacks/library'
import { isValidLottie, isValidSvg, parseManifest } from '../lib/appearancePacks/types'

const DIR = resolve(__dirname, 'fixtures/appearance-packs')

function load(name: string) {
  const text = readFileSync(resolve(DIR, `${name}.json`), 'utf-8')
  const parsed = bundleFromText(text)
  expect(parsed.ok).toBe(true)
  if (!parsed.ok) throw new Error('unreachable')
  const manifest = parseManifest(JSON.stringify(parsed.bundle.manifest))
  expect(manifest.ok).toBe(true)
  if (!manifest.ok) throw new Error('unreachable')
  return { text, bundle: parsed.bundle, manifest: manifest.value }
}

/** A PNG's magic bytes, so a sheet that stopped being a PNG is caught. */
const PNG_MAGIC = '\x89PNG\r\n\x1a\n'

/**
 * The RGBA bytes of a non-interlaced 8-bit truecolour+alpha PNG.
 *
 * Hand-rolled because this is the only reader in the suite and it needs exactly
 * one PNG shape — the one the sprite fixture is generated as. It inflates IDAT and
 * undoes the five per-scanline filters; anything else about the file is refused
 * loudly rather than guessed at, so a fixture regenerated in another shape fails
 * here instead of being silently misread.
 */
function decodePng(buf: Buffer): Buffer {
  expect(buf.subarray(0, 8).toString('latin1')).toBe(PNG_MAGIC)
  const width = buf.readUInt32BE(16)
  const height = buf.readUInt32BE(20)
  expect([buf[24], buf[25], buf[28]]).toEqual([8, 6, 0]) // 8-bit, RGBA, no interlace
  const idat: Buffer[] = []
  let at = 8
  while (at < buf.length) {
    const len = buf.readUInt32BE(at)
    const tag = buf.subarray(at + 4, at + 8).toString('latin1')
    if (tag === 'IDAT') idat.push(buf.subarray(at + 8, at + 8 + len))
    at += 12 + len
  }
  const raw = inflateSync(Buffer.concat(idat))
  const stride = width * 4
  const out = Buffer.alloc(height * stride)
  for (let y = 0; y < height; y++) {
    const filter = raw[y * (stride + 1)]
    const line = raw.subarray(y * (stride + 1) + 1, (y + 1) * (stride + 1))
    for (let x = 0; x < stride; x++) {
      const a = x >= 4 ? out[y * stride + x - 4] : 0
      const b = y > 0 ? out[(y - 1) * stride + x] : 0
      const c = x >= 4 && y > 0 ? out[(y - 1) * stride + x - 4] : 0
      let value = line[x]
      if (filter === 1) value += a
      else if (filter === 2) value += b
      else if (filter === 3) value += (a + b) >> 1
      else if (filter === 4) {
        const p = a + b - c
        const pa = Math.abs(p - a)
        const pb = Math.abs(p - b)
        const pc = Math.abs(p - c)
        value += pa <= pb && pa <= pc ? a : pb <= pc ? b : c
      } else if (filter !== 0) {
        throw new Error(`unsupported PNG filter ${filter} on row ${y}`)
      }
      out[y * stride + x] = value & 0xff
    }
  }
  return out
}

describe.each(['svg', 'lottie', 'sprite'] as const)('sample-%s', (format) => {
  it('is a bundle the importer accepts', () => {
    const { bundle, manifest } = load(`sample-${format}`)
    expect(bundle.kind).toBe(PACK_BUNDLE_KIND)
    // The envelope's `id` is where the importer takes the pack id from.
    expect(bundle.id).toBe(`sample-${format}`)
    expect(manifest.meta.format).toBe(format)
    expect(manifest.states.idle).toBeTruthy()
  })

  it('carries every file its slots name', () => {
    const { bundle, manifest } = load(`sample-${format}`)
    for (const file of Object.values(manifest.states)) {
      if (file) expect(bundle.files[file], file).toBeTruthy()
    }
  })

  it('stays small enough to read and to import', () => {
    const { text } = load(`sample-${format}`)
    expect(text.length).toBeLessThan(64 * 1024)
  })
})

describe('sample art is readable by the player that will draw it', () => {
  it('draws real svg for every slot of the svg pack', () => {
    const { bundle, manifest } = load('sample-svg')
    for (const file of Object.values(manifest.states)) {
      if (file) expect(isValidSvg(bundle.files[file] as string), file).toBe(true)
    }
  })

  it('draws lottie-web-valid json for every slot of the lottie pack', () => {
    const { bundle, manifest } = load('sample-lottie')
    for (const file of Object.values(manifest.states)) {
      if (file) expect(isValidLottie(bundle.files[file] as string), file).toBe(true)
    }
  })

  it('carries a two-row sheet whose rows the slots are assigned to', () => {
    const { bundle, manifest } = load('sample-sprite')
    expect(manifest.sprite?.rowAssignments).toEqual({ idle: 0, working: 1 })
    const sheet = Buffer.from(bundle.files['sheet.png'] as string, 'base64')
    expect(sheet.subarray(0, 8).toString('latin1')).toBe(PNG_MAGIC)
    // IHDR width/height, big-endian at byte 16 — two frames wide, two rows tall
    // at the frame size the manifest declares.
    expect(sheet.readUInt32BE(16)).toBe(manifest.sprite!.frameWidth * 2)
    expect(sheet.readUInt32BE(20)).toBe(manifest.sprite!.frameHeight * 2)
  })

  it('draws its two rows as two different SHAPES, not one shape twice', () => {
    // This fixture's whole job is to make "is the row being read?" visible. Two
    // rows that differ only in colour look identical to a row lookup that
    // silently resolved to row 0, so the sample proved nothing: idle and working
    // must differ in which PIXELS are opaque, not just in their fill.
    const { bundle, manifest } = load('sample-sprite')
    const { frameWidth: fw, frameHeight: fh } = manifest.sprite!
    const rgba = decodePng(Buffer.from(bundle.files['sheet.png'] as string, 'base64'))

    /** The opaque mask of one frame, as a string of '#' and '.'. */
    const mask = (col: number, row: number) => {
      const out: string[] = []
      for (let y = 0; y < fh; y++) {
        let line = ''
        for (let x = 0; x < fw; x++) {
          const at = ((row * fh + y) * fw * 2 + col * fw + x) * 4
          line += rgba[at + 3] > 10 ? '#' : '.'
        }
        out.push(line)
      }
      return out.join('\n')
    }

    const idle = mask(0, 0)
    const working = mask(0, 1)
    expect(idle).not.toEqual(working)
    // And each row must ANIMATE: two identical frames within a row would loop
    // as a still image.
    expect(mask(0, 0)).not.toEqual(mask(1, 0))
    expect(mask(0, 1)).not.toEqual(mask(1, 1))
  })
})
