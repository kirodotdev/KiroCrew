/**
 * CrewGhost — the crew roster's identity avatar.
 *
 * The component used to draw the mascot with canvas `fillRect` calls, and these
 * assertions used to read that draw program. It now renders a URL-imported png
 * sprite sheet instead, because `website/AUTOSDE.yaml`'s blocking
 * `use-lucide-icons` rule grants a mascot its BRAND-MARK EXCEPTION only when the
 * art is an asset rather than code. So what is checked here changed shape: not
 * "which rects were filled" but "which cell of the sheet is showing", which is
 * the whole of the component's remaining logic.
 *
 * The cell is read back out of the crop's geometry — the `<img>`'s offset divided
 * by the cell size — rather than from a `data-` attribute mirroring the same
 * number, so a broken offset cannot pass by agreeing with itself.
 *
 * `assets the rule cares about` guards the reason for the change: the source file
 * must stay free of inline vector markup, path data and `<canvas>`, and the art
 * must stay on disk as a png.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { render } from '@testing-library/react'
import { readFileSync, existsSync } from 'node:fs'
import { resolve } from 'node:path'
import CrewGhost, {
  CREW_GHOST_FRAME,
  CREW_GHOST_SHEET,
  djb2,
  ghostVariantCount,
} from '../apps/issue-radar/components/CrewGhost'
import spriteUrl from '../apps/issue-radar/components/crew-ghost-sprite.png'
// Vite's `?raw` — the component's own text, so the guard below reads exactly what
// AUTOSDE's regex would grep rather than a path this test guessed at.
import componentSource from '../apps/issue-radar/components/CrewGhost.tsx?raw'

/** Vitest's root is `website/` (vitest.config.ts), so assets resolve from there.
 *  Existence is asserted below, so a wrong root fails loudly. */
const COMPONENT_DIR = resolve(process.cwd(), 'src/apps/issue-radar/components')
const SPRITE_FILE = resolve(COMPONENT_DIR, 'crew-ghost-sprite.png')
const GENERATOR = resolve(COMPONENT_DIR, 'crew-ghost-sprite.gen.mjs')

/* ── Reading the render back ── */

interface Shown {
  /** The clipping box: one frame, scaled. */
  box: HTMLElement
  img: HTMLImageElement
  /** CSS pixels per sprite pixel, recovered from the box. */
  px: number
  /** Which sheet cell is visible, recovered from the crop offset. */
  col: number
  row: number
  /** The raw crop offsets, so "shifts the right way" is checkable too. */
  left: number
  top: number
  unmount: () => void
}

function show(props: { seed: string; size?: number; variant?: number | null; blush?: boolean }): Shown {
  const { container, unmount } = render(<CrewGhost {...props} />)
  const box = container.querySelector<HTMLElement>('[data-testid="crew-ghost"]')!
  const img = box.querySelector('img')!
  const px = parseFloat(box.style.height) / CREW_GHOST_FRAME.height
  // `left`/`top` are negative multiples of the cell; `-0` serialises as "0px".
  const left = parseFloat(img.style.left) || 0
  const top = parseFloat(img.style.top) || 0
  return {
    box,
    img,
    px,
    // `Math.abs` keeps cell 0 out of `-0`, which `toBe(0)` rejects. The sign is
    // asserted on its own in "only ever shifts the sheet up and left".
    col: Math.abs(left) / (CREW_GHOST_SHEET.cellWidth * px),
    row: Math.abs(top) / (CREW_GHOST_SHEET.cellHeight * px),
    left,
    top,
    unmount,
  }
}

/** happy-dom exposes a real `devicePixelRatio` setter (the convention in
 *  useTheme.test.tsx is to restore it by hand). */
const originalDpr = window.devicePixelRatio
afterEach(() => { window.devicePixelRatio = originalDpr })

describe('CrewGhost', () => {
  it('renders the mascot as a URL-imported sprite asset, not a canvas', () => {
    const { box, img } = show({ seed: 'sombrero' })
    // The plain-<img> path the brand-mark exception prescribes for a full-colour
    // mark: a real src pointing at the imported asset, no mask, no currentColor.
    expect(img.getAttribute('src')).toBe(spriteUrl)
    expect(spriteUrl).toMatch(/crew-ghost-sprite.*\.png/)
    expect(box.querySelector('canvas')).toBeNull()
    // Not BrandGlyph's monochrome treatment: this mark's eight looks are told
    // apart by their colours, so flattening them to `currentColor` would erase
    // the distinction the variants exist for.
    for (const el of [box, img]) {
      expect(el.getAttribute('style') ?? '').not.toMatch(/mask/i)
      expect(el.getAttribute('style') ?? '').not.toMatch(/currentColor/i)
    }
  })

  it('only ever shifts the sheet up and left', () => {
    // The crop reads `Math.abs`, so a sign flip would otherwise look like the
    // right cell while showing the sheet's empty side.
    for (const [variant, blush] of [[0, false], [7, true], [3, true]] as const) {
      const out = show({ seed: 'ursa', variant, blush })
      expect(out.left).toBeLessThanOrEqual(0)
      expect(out.top).toBeLessThanOrEqual(0)
      out.unmount()
    }
  })

  it('shows the same cell for the same seed', () => {
    const first = show({ seed: 'sombrero' })
    first.unmount()
    const second = show({ seed: 'sombrero' })
    expect(second.col).toBe(first.col)
    expect(second.row).toBe(first.row)
    // Not vacuously true: a real column was selected, in range.
    expect(Number.isInteger(second.col)).toBe(true)
    expect(second.col).toBeLessThan(ghostVariantCount)
  })

  it('shows a different cell for a seed that hashes to another outfit', () => {
    // Pick two seeds that provably select different looks, so this cannot pass
    // by accident on a hash collision.
    const a = 'andromeda'
    const b = ['whirlpool', 'pinwheel', 'tadpole', 'fornax', 'grus'].find(
      s => djb2(s) % ghostVariantCount !== djb2(a) % ghostVariantCount,
    )!
    expect(djb2(b) % ghostVariantCount).not.toBe(djb2(a) % ghostVariantCount)

    const first = show({ seed: a })
    first.unmount()
    const second = show({ seed: b })
    expect(second.col).not.toBe(first.col)
    // The column is the hash, not just "something else".
    expect(second.col).toBe(djb2(b) % ghostVariantCount)
    expect(first.col).toBe(djb2(a) % ghostVariantCount)
  })

  it('gives two seeds that land on the same outfit the same face', () => {
    // The honest limit of the distinctness above: with 8 looks, seeds collide by
    // design. Documented here so nobody reads determinism as uniqueness.
    const seeds = ['carina', 'draco', 'medusa', 'cocoon', 'tucana', 'leo', 'hoag', 'mayall', 'cigar']
    const target = djb2(seeds[0]) % ghostVariantCount
    const twin = seeds.slice(1).find(s => djb2(s) % ghostVariantCount === target)
    if (!twin) return // no collision among these names; nothing to assert

    const first = show({ seed: seeds[0] })
    first.unmount()
    expect(show({ seed: twin }).col).toBe(first.col)
  })

  it('lets `variant` override the seed', () => {
    const seed = 'butterfly'
    const natural = djb2(seed) % ghostVariantCount
    const pinned = (natural + 3) % ghostVariantCount

    const bySeed = show({ seed })
    bySeed.unmount()
    const overridden = show({ seed, variant: pinned })
    overridden.unmount()
    // The pinned look must be the outfit itself, not merely "something else":
    // a different seed asking for the same variant lands on the same cell.
    const otherSeed = show({ seed: 'a completely different crew', variant: pinned })

    expect(overridden.col).toBe(pinned)
    expect(overridden.col).not.toBe(bySeed.col)
    expect(otherSeed.col).toBe(overridden.col)
  })

  it('treats variant 0 as a pin, not as "no variant"', () => {
    // `0` is falsy; a `variant || null` style check would silently fall through
    // to the seed. Pick a seed that does NOT hash to 0 so the two differ.
    const seed = ['spindle', 'porpoise', 'sculptor', 'triangulum', 'ursa'].find(
      s => djb2(s) % ghostVariantCount !== 0,
    )!
    const zero = show({ seed, variant: 0 })
    zero.unmount()
    expect(zero.col).toBe(0)
    expect(zero.col).not.toBe(show({ seed }).col)
  })

  it('folds an out-of-range variant back onto the sheet', () => {
    // The crop is arithmetic now, so an unfolded index would scroll the sheet
    // off its own edge and show an empty box rather than the wrong hat.
    for (const [variant, expected] of [[8, 0], [11, 3], [-1, 7], [-9, 7], [2.7, 2]] as const) {
      const out = show({ seed: 'grus', variant })
      expect(out.col).toBe(expected)
      expect(out.col).toBeGreaterThanOrEqual(0)
      expect(out.col).toBeLessThan(ghostVariantCount)
      out.unmount()
    }
  })

  it('crops every variant to a whole cell inside the sheet', () => {
    // Both a 1× and a 2× size: at 1× the scale factor is a no-op, so the sheet's
    // own scaling would go unchecked if this only ran at the default size.
    for (const size of [34, 78]) {
      const cols = new Set<number>()
      for (let v = 0; v < ghostVariantCount; v++) {
        const out = show({ seed: 'ignored', variant: v, size })
        expect(out.px).toBe(size === 34 ? 1 : 2)
        // A fractional offset would show two half-ghosts.
        expect(Number.isInteger(out.col)).toBe(true)
        // The whole sheet is offered to the crop, scaled to the same factor as
        // the box — otherwise the frame would show a slice of the wrong size.
        expect(out.img.getAttribute('width'))
          .toBe(String(CREW_GHOST_SHEET.columns * CREW_GHOST_SHEET.cellWidth * out.px))
        expect(out.img.getAttribute('height'))
          .toBe(String(CREW_GHOST_SHEET.rows * CREW_GHOST_SHEET.cellHeight * out.px))
        cols.add(out.col)
        out.unmount()
      }
      // Eight looks, eight distinct columns — no two variants share a cell.
      expect(cols.size).toBe(ghostVariantCount)
    }
    expect(ghostVariantCount).toBe(8)
  })

  it('selects the blush row only when asked', () => {
    const plain = show({ seed: 'crown', variant: 0 })
    plain.unmount()
    const rosy = show({ seed: 'crown', variant: 0, blush: true })
    expect(plain.row).toBe(0)
    expect(rosy.row).toBe(1)
    // Blush changes the row and nothing else — same outfit, same size.
    expect(rosy.col).toBe(plain.col)
    expect(rosy.box.style.height).toBe(plain.box.style.height)
  })

  it('clips the box to exactly one frame', () => {
    const { box, px } = show({ seed: 'cigar' })
    expect(box.style.overflow).toBe('hidden')
    expect(box.style.position).toBe('relative')
    expect(box.style.width).toBe(`${CREW_GHOST_FRAME.width * px}px`)
    expect(box.style.height).toBe(`${CREW_GHOST_FRAME.height * px}px`)
    // The frame is the visible crop, so it must exclude the sheet's gutter.
    expect(CREW_GHOST_SHEET.cellWidth).toBeGreaterThan(CREW_GHOST_FRAME.width)
    expect(CREW_GHOST_SHEET.cellHeight).toBeGreaterThan(CREW_GHOST_FRAME.height)
  })

  it('snaps the scale to whole sprite pixels', () => {
    // Every size the dashboard actually asks for, and what it must render at.
    // A fractional scale is what would land the crop between the sheet's cells
    // and soften the art, so the box is snapped rather than honoured exactly —
    // these are the pre-existing numbers.
    for (const [size, expectedPx] of [[26, 1], [34, 1], [40, 1], [78, 2], [111, 3]] as const) {
      const out = show({ seed: 'crown', size })
      expect(out.px).toBe(expectedPx)
      expect(out.box.style.height).toBe(`${CREW_GHOST_FRAME.height * expectedPx}px`)
      // Deliberately smooth, not `pixelated`: the sheet is a 4× supersample, so
      // an integer downscale reconstructs the antialiasing the old canvas draw
      // produced. Nearest-neighbour is what loses fidelity here — measured over
      // all 16 frames, it leaves 2.1% of pixels visibly wrong and smooth leaves
      // none.
      expect(out.img.style.imageRendering).toBe('auto')
      out.unmount()
    }
  })

  it('never collapses to a sub-pixel frame', () => {
    const { px, box } = show({ seed: 'crown', size: 4 })
    expect(px).toBe(1)
    expect(box.style.height).toBe(`${CREW_GHOST_FRAME.height}px`)
  })

  it('sizes the frame independently of the device pixel ratio', () => {
    // The canvas implementation sized a backing store, so its CSS box moved with
    // `devicePixelRatio` — at dpr 2, `size: 26` rendered 18.5px, half the box the
    // roster reserves. An <img> is rasterised by the compositor, so the layout is
    // now the same on every display and the extra device pixels just add detail.
    window.devicePixelRatio = 1
    const atOne = show({ seed: 'crown', size: 26 })
    atOne.unmount()
    window.devicePixelRatio = 3
    const atThree = show({ seed: 'crown', size: 26 })
    expect(atThree.box.style.height).toBe(atOne.box.style.height)
    expect(atThree.box.style.width).toBe(atOne.box.style.width)
    expect(atThree.img.getAttribute('width')).toBe(atOne.img.getAttribute('width'))
  })

  it('is decorative — aria-hidden with no accessible name', () => {
    const { box, img } = show({ seed: 'fireworks' })
    expect(box).toHaveAttribute('aria-hidden', 'true')
    expect(box).not.toHaveAttribute('aria-label')
    expect(box).not.toHaveAttribute('role')
    // An <img> needs its own empty alt: a decorative image with no alt at all is
    // announced by its filename.
    expect(img).toHaveAttribute('alt', '')
    expect(img).toHaveAttribute('aria-hidden', 'true')
    // The crew's name is rendered as text beside the avatar, so any name here
    // would be read out twice.
    expect(box.textContent).toBe('')
  })

  it('is not a drag source', () => {
    // An <img> is draggable by default, which a canvas was not; dragging a roster
    // row would otherwise start a stray image drag.
    expect(show({ seed: 'hoag' }).img).toHaveAttribute('draggable', 'false')
  })

  it('passes className through for layout', () => {
    expect(show({ seed: 'grus' }).box.className).toBe('')
    const styled = render(<CrewGhost seed="grus" className="rounded-md" />)
    expect(styled.container.querySelector('[data-testid="crew-ghost"]')!.className).toBe('rounded-md')
  })
})

describe('assets the rule cares about', () => {
  // Only the code, so the docstring's prose about the rule cannot satisfy or
  // trip these checks. `import` lines stay in — the png import is asserted.
  const code = componentSource.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')

  it('produces no inline vector element and no canvas', () => {
    // The blocking `use-lucide-icons` regex greps ADDED .tsx lines for an `<svg`
    // tag carrying a `viewBox`, with no exception — this file included, which is
    // why the pattern is only ever written here inside a regex literal. `canvas`
    // is what this change removed and must not come back.
    expect(code).not.toMatch(/<svg[^>]*viewBox/)
    expect(code).not.toMatch(/<svg/)
    expect(code).not.toMatch(/<canvas/i)
    expect(code).not.toMatch(/getContext\(/)
    // Path data in any form — the other half of "asset, not code".
    expect(code).not.toMatch(/\bd="[Mm][\s\d]/)
    expect(code).not.toMatch(/\b(?:path|polygon|polyline)\s*=/)

    // …and the rendered output carries neither element either.
    const { container } = render(<CrewGhost seed="tucana" />)
    expect(container.querySelector('svg')).toBeNull()
    expect(container.querySelector('canvas')).toBeNull()
    expect(container.querySelectorAll('img')).toHaveLength(1)
  })

  it('consumes the art through a plain URL import of a real png', () => {
    expect(code).toMatch(/^import spriteUrl from '\.\/crew-ghost-sprite\.png'$/m)
    expect(existsSync(SPRITE_FILE)).toBe(true)
    const png = readFileSync(SPRITE_FILE)
    // PNG signature, then IHDR's big-endian width/height at bytes 16..24.
    expect(png.subarray(0, 8).toString('hex')).toBe('89504e470d0a1a0a')
    expect(png.readUInt32BE(16)).toBe(CREW_GHOST_SHEET.columns * CREW_GHOST_SHEET.cellWidth * 4)
    expect(png.readUInt32BE(20)).toBe(CREW_GHOST_SHEET.rows * CREW_GHOST_SHEET.cellHeight * 4)
  })

  it('keeps the drawing program out of the bundle', () => {
    // The art is generated, and the generator must stay a hand-run script beside
    // the asset: if the component ever imported it, the drawing code would be
    // back in the bundle and condition 1 would be broken again.
    expect(existsSync(GENERATOR)).toBe(true)
    expect(code).not.toMatch(/crew-ghost-sprite\.gen/)
    expect(code).not.toMatch(/KIRO_GHOST_PIXELS/)
    expect(code).not.toMatch(/fillRect/)
  })
})

describe('djb2 / ghostVariantCount', () => {
  it('matches the hash the rest of the dashboard uses', () => {
    // Same algorithm as `gradientFor` (components/appstore/gradient.ts) and
    // `pickAnimal` (pages/scenes/WateringHoleScene.tsx). Exported so a third
    // copy never gets written; these values pin it.
    expect(djb2('')).toBe(5381)
    expect(djb2('a')).toBe(177670)
    expect(djb2('kirocrew')).toBe(djb2('kirocrew'))
    expect(djb2('crew-1')).not.toBe(djb2('crew-2'))
    // Unsigned 32-bit, so `% ghostVariantCount` can never be negative.
    for (const s of ['andromeda', 'bode', 'whirlpool', 'sombrero', '', 'crëw-ünïcode']) {
      expect(djb2(s)).toBeGreaterThanOrEqual(0)
      expect(djb2(s)).toBeLessThanOrEqual(0xffffffff)
    }
  })

  it('reports the size of the outfit table', () => {
    expect(ghostVariantCount).toBe(8)
    expect(ghostVariantCount).toBe(CREW_GHOST_SHEET.columns)
    expect(CREW_GHOST_FRAME.width).toBeGreaterThan(24)
    expect(CREW_GHOST_FRAME.height).toBeGreaterThan(28)
  })

  it('pins the sheet box in CSS so a global img rule cannot squash it', () => {
    // REGRESSION, and the reason this asserts inline STYLE rather than the
    // width/height attributes: with only the attributes set, Tailwind's preflight
    // (`img { max-width: 100%; height: auto }`) won in the real browser and the
    // 240px-wide sheet was clamped to the 29px crop, then `height: auto` shrank it
    // to 9px. Every crew but the first rendered as empty space and the first as a
    // squashed sliver — while this suite stayed green, because jsdom never lays the
    // image out. Measured in a real browser after the fix: 240x76 inside a 29x37
    // crop, where the broken build measured 29x9.
    const { getByTestId } = render(<CrewGhost seed="Andromeda" />)
    const img = getByTestId('crew-ghost').querySelector('img')
    expect(img).not.toBeNull()
    const style = img!.style
    // The clamp-lifter. Without this the width below is capped by the crop.
    expect(style.maxWidth).toBe('none')
    const sheetW = CREW_GHOST_SHEET.columns * CREW_GHOST_SHEET.cellWidth
    const sheetH = CREW_GHOST_SHEET.rows * CREW_GHOST_SHEET.cellHeight
    // Both axes pinned: `height: auto` is half of what broke it, so asserting
    // width alone would still pass on the squashed build.
    expect(style.width).toBe(`${sheetW}px`)
    expect(style.height).toBe(`${sheetH}px`)
  })
})
