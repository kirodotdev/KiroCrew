/**
 * The `mc-touch-hit*` classes grow a small icon control's HIT AREA to 44px on a
 * coarse pointer, and do nothing at all for a mouse.
 *
 * Asserted against SOURCE TEXT, as scrollbarOverlayTouch.test.ts and
 * noPageZoom.test.ts do for this stylesheet: jsdom never loads index.css, and
 * a media query has no computed representation to read even if it did. The
 * geometry is then evaluated from the declared `inset` values, which is how the
 * browser resolves them (top/bottom percentages against the host's height,
 * left/right against its width).
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const CSS = readFileSync(resolve(__dirname, '../index.css'), 'utf8')

/** The `@layer components { @media (pointer: coarse) { ... } }` block that owns the classes. */
const BLOCK = /@layer components \{\n {2}@media \(pointer: coarse\) \{\n([\s\S]*?)\n {2}\}\n\}/.exec(CSS)
const BODY = BLOCK?.[1] ?? ''

/** Declarations of the rule whose selector list is exactly `selector`. */
function decls(selector: string): Record<string, string> {
  const esc = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const m = new RegExp(`(?:^|\\n)\\s*${esc}\\s*\\{([^}]*)\\}`).exec(BODY)
  expect(m, `rule "${selector}" not found in the coarse-pointer block`).not.toBeNull()
  const out: Record<string, string> = {}
  for (const d of m![1].split(';')) {
    const i = d.indexOf(':')
    if (i > 0) out[d.slice(0, i).trim()] = d.slice(i + 1).trim()
  }
  return out
}

/** Evaluate `0`, `min(0px, calc(P% - Npx))` against a host dimension. */
function resolveInset(value: string, size: number): number {
  if (value === '0') return 0
  const m = /^min\(0px, calc\((\d+)% - (\d+)px\)\)$/.exec(value)
  expect(m, `unexpected inset value "${value}"`).not.toBeNull()
  return Math.min(0, (Number(m![1]) / 100) * size - Number(m![2]))
}

type Rect = { left: number; right: number; top: number; bottom: number }

/** The ::after hit rect of a host box for one variant, in LTR. */
function hitRect(variant: 'mc-touch-hit' | 'mc-touch-hit-y' | 'mc-touch-hit-end', box: Rect): Rect {
  const base = decls('.mc-touch-hit::after, .mc-touch-hit-y::after, .mc-touch-hit-end::after')
  const own = variant === 'mc-touch-hit' ? {} : decls(`.${variant}::after`)
  const w = box.right - box.left
  const h = box.bottom - box.top
  const block = resolveInset(base.inset, h)
  const start = own['inset-inline-start'] ?? own['inset-inline'] ?? base.inset
  const end = own['inset-inline-end'] ?? own['inset-inline'] ?? base.inset
  return {
    top: box.top + block,
    bottom: box.bottom - block,
    left: box.left + resolveInset(start, w),
    right: box.right - resolveInset(end, w),
  }
}

describe('mc-touch-hit: 44px hit area on a coarse pointer only', () => {
  it('lives in one coarse-pointer block inside @layer components', () => {
    expect(BLOCK, 'no `@layer components { @media (pointer: coarse) { ... } }` block').not.toBeNull()
    // Every rule naming the classes is inside that block; none leaks to a mouse.
    const outside = CSS.replace(BLOCK![0], '')
    expect(outside).not.toMatch(/\.mc-touch-hit[\w-]*\s*[,{:]/)
    // The layer is load-bearing: the unlayered Tailwind utilities must beat the
    // `position: relative` below, so an absolute/sticky host keeps its position.
    expect(decls('.mc-touch-hit, .mc-touch-hit-y, .mc-touch-hit-end').position).toBe('relative')
    const after = decls('.mc-touch-hit::after, .mc-touch-hit-y::after, .mc-touch-hit-end::after')
    expect(after.content).toBe("''")
    expect(after.position).toBe('absolute')
  })

  it('reaches 44x44 on a small host and adds nothing on a large one', () => {
    const small = hitRect('mc-touch-hit', { left: 0, right: 24, top: 0, bottom: 24 })
    expect(small.right - small.left).toBe(44)
    expect(small.bottom - small.top).toBe(44)
    // Centred: equal outset on each side.
    expect(small.left).toBe(-10)
    expect(small.top).toBe(-10)
    const large = hitRect('mc-touch-hit', { left: 0, right: 80, top: 0, bottom: 48 })
    expect(large).toEqual({ left: 0, right: 80, top: 0, bottom: 48 })
  })

  it('-y grows height only; -end grows width toward the inline end only', () => {
    const y = hitRect('mc-touch-hit-y', { left: 0, right: 28, top: 0, bottom: 28 })
    expect(y).toEqual({ left: 0, right: 28, top: -8, bottom: 36 })
    const end = hitRect('mc-touch-hit-end', { left: 0, right: 24, top: 0, bottom: 28 })
    expect(end).toEqual({ left: 0, right: 44, top: -8, bottom: 36 })
  })

  it("keeps the split New button's two segments from overlapping", () => {
    // Sidebar header split button, compact and full: main segment, 1px
    // divider, 24px caret, all 28px tall.
    for (const mainWidth of [28, 56]) {
      const main = hitRect('mc-touch-hit-y', { left: 0, right: mainWidth, top: 0, bottom: 28 })
      const caretLeft = mainWidth + 1
      const caret = hitRect('mc-touch-hit-end', { left: caretLeft, right: caretLeft + 24, top: 0, bottom: 28 })
      expect(main.right).toBeLessThanOrEqual(caret.left)
      expect(main.bottom - main.top).toBe(44)
      expect(caret.bottom - caret.top).toBe(44)
      expect(caret.right - caret.left).toBe(44)
    }
  })
})
