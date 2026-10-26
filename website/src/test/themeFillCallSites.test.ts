/**
 * Text on a solid theme fill is inked with that fill's own `-fg` token, never a
 * hard-coded white.
 *
 * themeFillForeground.test.ts proves each pair (`--accent`/`--accent-fg`, and
 * the same for warn, danger, ok and info) clears AA in every theme bar the
 * amoled pairs it records; this file pins that the call sites actually use the
 * pair. White only reads on a dark fill, and most dark palettes pick light
 * ones: white on `--accent` fails WCAG AA (4.5:1) in 23 of the 40 theme
 * variants, Amber dark's `#ffd230` at 1.45:1, white on `--danger` fails in 25,
 * on `--ok` in 34 and on `--info` in 30.
 *
 * Asserted over source: the defect is a class or style literal, and jsdom
 * resolves no theme colours, so a render could not show it. A solid fill is
 * `bg-danger` or `bg-[var(--danger)]`, not the `bg-danger/10` or
 * `bg-danger-subtle` tints, which take the `-text` tokens instead. Not caught:
 * a fill class with an inline-style white, and a pair split across lines or
 * variables (none exist today).
 */
import { describe, it, expect } from 'vitest'
import { readdirSync } from 'node:fs'
import { join, relative, resolve, sep } from 'node:path'
import { readSource } from './readSource'

const ROOT = resolve(__dirname, '..')
const TIERS = 'accent|warn|danger|ok|info'
const SOLID = `!?bg-(?:(?:${TIERS})(?![\\w/-])|\\[var\\(--(?:${TIERS})\\)\\])`
const WHITE = '(?<![\\w-])!?text-white(?![\\w/-])'
/** A solid fill and white text in one class string, in either order. */
const CLASS_PAIR = new RegExp(`${SOLID}[^"'\`]*${WHITE}|${WHITE}[^"'\`]*${SOLID}`)
/** A solid fill in an inline style object... */
const STYLE_FILL = new RegExp(`\\bbackground(?:Color)?:\\s*['"]var\\(--(?:${TIERS})\\)['"]`)
/** ...with white text in the same object, or in the element's class list. */
const STYLE_WHITE = /\bcolor:\s*['"](?:#fff|#ffffff|white)['"]/i
const CLASS_WHITE = new RegExp(WHITE)

function offenders(): string[] {
  const out: string[] = []
  for (const entry of readdirSync(ROOT, { recursive: true, withFileTypes: true })) {
    if (!entry.isFile() || !/\.tsx?$/.test(entry.name) || /\.test\.tsx?$/.test(entry.name)) continue
    const path = join(entry.parentPath ?? entry.path, entry.name)
    const rel = relative(ROOT, path).split(sep).join('/')
    if (rel.startsWith('test/')) continue
    const text = readSource(path)
    const lineAt = (i: number) => text.slice(0, i).split('\n').length
    text.split('\n').forEach((line, i) => {
      if (CLASS_PAIR.test(line)) out.push(`${rel}:${i + 1}`)
    })
    for (const m of text.matchAll(/\{[^{}]*\}/g)) {
      if (!STYLE_FILL.test(m[0])) continue
      const white = STYLE_WHITE.exec(m[0])
      if (white) { out.push(`${rel}:${lineAt(m.index + white.index)}`); continue }
      // The opening tag this style belongs to, for a class-list white.
      const tag = text.slice(text.lastIndexOf('<', m.index), m.index)
      if (CLASS_WHITE.test(tag)) out.push(`${rel}:${lineAt(m.index)}`)
    }
  }
  return out
}

describe('text on a solid theme fill', () => {
  it('anchors the premise: the patterns catch the pair and pass the token', () => {
    expect(CLASS_PAIR.test('px-3 bg-accent text-white')).toBe(true)
    expect(CLASS_PAIR.test('text-white rounded bg-danger hover:opacity-80')).toBe(true)
    expect(CLASS_PAIR.test('!bg-[var(--warn)] !text-white')).toBe(true)
    expect(CLASS_PAIR.test('bg-accent text-accent-fg')).toBe(false)
    expect(CLASS_PAIR.test('bg-danger/10 text-white')).toBe(false)
    expect(CLASS_PAIR.test('bg-danger-subtle text-white')).toBe(false)
    expect(STYLE_FILL.test("background: 'var(--danger)'") && STYLE_WHITE.test("color: '#fff'")).toBe(true)
    expect(STYLE_WHITE.test("color: 'var(--danger-fg)'")).toBe(false)
  })

  it("inks every solid accent, warn, danger, ok and info fill with the fill's own -fg token", () => {
    expect(offenders()).toEqual([])
  })
})
