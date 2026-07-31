/**
 * Script fallback faces must lead every font stack, and must never claim Latin.
 *
 * zh-CN, hi and bn have no font coverage otherwise: every family in `--font-body`
 * and `--mono` covers Latin only, so those scripts fall through to the browser's
 * per-script fallback, which picks a face per character and silently mismatches.
 * CJK punctuation is the visible symptom — Unicode uses one code point for the
 * Chinese and Japanese comma and full stop, so only the font decides where in the
 * em box the glyph sits.
 *
 * The mechanism is four `unicode-range`-restricted `@font-face` aliases over
 * locally installed faces, placed at the FRONT of each stack. Two properties make
 * that correct, and both are asserted here because neither is visible from reading
 * a family list:
 *
 *  1. **No alias range may include Latin.** `unicode-range` is what makes a leading
 *     position safe: a face whose range excludes Latin is never consulted for
 *     Latin, so leading cannot change how Latin renders. If someone widens a range
 *     to cover Latin, every stack in the app silently switches its Latin face.
 *     This is the assertion that matters most.
 *  2. **Every declaration site must reference the alias token.** The stacks are
 *     declared in TWELVE places across three files — nine `--font-body`/`--mono`
 *     declarations (`index.css` 4, `hooks/useTheme.tsx` 5) plus the three
 *     `FAMILY_MAP` entries in `hooks/useZoom.ts` that are written into `--font-body`
 *     at runtime. A thirteenth added later without the aliases would silently lose
 *     script coverage on whichever path it feeds, so the check globs the tree rather
 *     than naming files.
 *  3. **Each alias needs a REAL bold face.** Weight matching happens *within* the
 *     selected family, so an alias backed by one Regular face makes every
 *     `font-semibold` in these scripts render as Chromium's synthetic bold — worse
 *     than today, where browser fallback reaches the platform's real Semibold. Two
 *     faces per alias (400 and 700) is what keeps the real face, and dropping the
 *     700 later would silently reintroduce faux-bold.
 *
 * Why not simply append the script families to the end of each stack: every stack
 * already terminates in a generic (`sans-serif` / `monospace`), and on macOS
 * `-apple-system` resolves Han from the OS cascade before any appended family is
 * reached — so an appended tail is platform-dependent, and possibly a no-op. A
 * leading unicode-ranged alias is deterministic regardless of what follows it.
 */

import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'

import { describe, it, expect } from 'vitest'

const SRC = join(__dirname, '..')
const INDEX_CSS = readFileSync(join(SRC, 'index.css'), 'utf8')

/** The alias faces, and the script each one covers. */
const ALIASES = [
  'KC Han Fallback',
  'KC Han Mono Fallback',
  'KC Devanagari Fallback',
  'KC Bengali Fallback',
] as const

/**
 * Ranges that must stay OUT of every alias. Latin proper plus general punctuation:
 * an alias claiming U+2000-206F would take over quotes, dashes and ellipses in
 * Latin text, which is the same class of silent regression as claiming Latin.
 */
const FORBIDDEN = [
  { name: 'Latin (Basic through Extended-B)', lo: 0x0000, hi: 0x024f },
  { name: 'General Punctuation', lo: 0x2000, hi: 0x206f },
]

/**
 * The Latin base families. Aliases must precede all of them in every stack.
 *
 * Known limit, stated so this gate is not read as complete coverage: the check is
 * LINE-scoped, so a stack wrapped across lines by a formatter, or a family name not
 * listed here, would pass. It catches the realistic regression — someone appending
 * the token instead of leading with it — not every possible ordering mistake.
 */
const BASE_FAMILIES = [
  "'Space Grotesk'",
  '-apple-system',
  'BlinkMacSystemFont',
  "'JetBrains Mono'",
  'ui-monospace',
  'SFMono-Regular',
  "'Segoe UI'",
  'Menlo',
  'sans-serif',
  'monospace',
]

/** Every @font-face block declaring `family`, one per weight. */
function faceBlocks(family: string): string[] {
  // @font-face blocks here contain no nested braces, so a non-greedy match to the
  // first `}` is sufficient. Asserted non-empty by the first test.
  const re = new RegExp(`@font-face\\s*\\{([^}]*?font-family:\\s*'${family}'[^}]*?)\\}`, 'g')
  return [...INDEX_CSS.matchAll(re)].map((m) => m[1])
}

function faceBlock(family: string): string {
  return faceBlocks(family).join('\n')
}

function parseUnicodeRange(block: string): Array<{ lo: number; hi: number }> {
  const decl = block.match(/unicode-range:\s*([^;}]+)/)
  if (!decl) return []
  return decl[1]
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
    .map((token) => {
      const m = token.match(/^U\+([0-9A-Fa-f]+)(?:-([0-9A-Fa-f]+))?$/)
      if (!m) throw new Error(`unparseable unicode-range token: "${token}"`)
      const lo = parseInt(m[1], 16)
      return { lo, hi: m[2] ? parseInt(m[2], 16) : lo }
    })
}

/** Every file that DECLARES --font-body or --mono, found by walking the tree. */
function declarationSites(): Array<{ file: string; line: number; text: string }> {
  const out: Array<{ file: string; line: number; text: string }> = []
  const walk = (dir: string) => {
    for (const entry of readdirSync(dir)) {
      if (entry === 'node_modules' || entry.startsWith('.')) continue
      const full = join(dir, entry)
      if (statSync(full).isDirectory()) {
        walk(full)
        continue
      }
      if (!/\.(css|ts|tsx)$/.test(entry)) continue
      // Tests never declare a font stack, and this file necessarily contains the
      // detection pattern as a literal — without the exclusion it matches itself.
      // The only false-negative this creates is a stack declared inside a test,
      // which would not reach the app.
      if (/\.test\.(ts|tsx)$/.test(entry)) continue
      readFileSync(full, 'utf8')
        .split('\n')
        .forEach((text, i) => {
          // A declaration, not a read: `--font-body:` / `--mono:` with a value, and
          // the FAMILY_MAP entries that are written into --font-body at runtime.
          const declares = /--(?:font-body|mono)\s*:/.test(text)
          const familyMap = /^\s*(?:sans|mono|system):\s*"/.test(text)
          if (declares || familyMap) out.push({ file: relative(SRC, full), line: i + 1, text })
        })
    }
  }
  walk(SRC)
  return out
}

describe('script fallback faces', () => {
  it.each(ALIASES)('defines %s with a unicode-range and local()-only sources', (family) => {
    const block = faceBlock(family)
    expect(block, `no @font-face for '${family}' in index.css`).not.toBe('')
    expect(block).toMatch(/unicode-range:/)
    expect(block).toMatch(/src:[^;]*local\(/)
    // local() only: a url() here would make the dashboard fetch a font at runtime.
    expect(block, `'${family}' must not fetch a remote font`).not.toMatch(/url\(/)
  })

  it.each(ALIASES)('keeps %s out of Latin and general punctuation', (family) => {
    const ranges = parseUnicodeRange(faceBlock(family))
    expect(ranges.length, `'${family}' declares no parseable unicode-range`).toBeGreaterThan(0)
    for (const r of ranges) {
      for (const bad of FORBIDDEN) {
        const overlaps = r.lo <= bad.hi && r.hi >= bad.lo
        expect(
          overlaps,
          `'${family}' range U+${r.lo.toString(16).toUpperCase()}-${r.hi
            .toString(16)
            .toUpperCase()} overlaps ${bad.name}; a leading alias must never claim it`,
        ).toBe(false)
      }
    }
  })

  it.each(ALIASES)('backs %s with a real 400 and 700 face, not one 100-900 face', (family) => {
    const blocks = faceBlocks(family)
    const weights = blocks
      .map((b) => b.match(/font-weight:\s*([^;]+);/)?.[1].trim())
      .filter(Boolean)
      .sort()
    expect(weights, `'${family}' must declare exactly two weights`).toEqual(['400', '700'])
    // A range like `100 900` on a single Regular face is the faux-bold trap.
    for (const w of weights) expect(w).not.toMatch(/\s/)
    // Both weights must cover the same code points, or bold text falls out of the alias.
    const [a, b] = blocks.map((blk) => blk.match(/unicode-range:\s*([^;}]+)/)?.[1].replace(/\s+/g, ' '))
    expect(a, `'${family}' weights disagree on unicode-range`).toBe(b)
  })

  it('declares both tokens in :root so every consumer inherits them', () => {
    // If either moved into a [data-theme=…] block or was renamed, every --font-body
    // would become guaranteed-invalid at computed-value time and `font-family:
    // var(--mono)` consumers would drop to their initial value — app-wide font loss,
    // which every other assertion here would still pass through.
    const root = INDEX_CSS.match(/:root\s*\{([^}]*)\}/)?.[1] ?? ''
    expect(root, 'no :root block found in index.css').not.toBe('')
    expect(root).toMatch(/--script-fallbacks:/)
    expect(root).toMatch(/--script-fallbacks-mono:/)
  })

  it('lists every alias in the shared tokens', () => {
    const root = INDEX_CSS.match(/--script-fallbacks:\s*([^;]+);/)?.[1] ?? ''
    const rootMono = INDEX_CSS.match(/--script-fallbacks-mono:\s*([^;]+);/)?.[1] ?? ''
    expect(root, 'no --script-fallbacks token').not.toBe('')
    expect(rootMono, 'no --script-fallbacks-mono token').not.toBe('')
    for (const family of ALIASES) {
      if (family === 'KC Han Mono Fallback') {
        // Mono-only: a proportional stack must not lead with a monospace face.
        expect(rootMono).toContain(family)
        expect(root).not.toContain(family)
      } else {
        expect(root, `${family} missing from --script-fallbacks`).toContain(family)
        expect(rootMono, `${family} missing from --script-fallbacks-mono`).toContain(family)
      }
    }
  })
})

describe('font stack declarations', () => {
  it('finds every declaration site the tree actually contains', () => {
    // Guards the walker itself: if this drops to a handful, the glob broke and the
    // next assertion would pass vacuously.
    expect(declarationSites().length).toBeGreaterThanOrEqual(12)
  })

  it('references the alias token at every declaration site', () => {
    const offenders = declarationSites()
      .filter((s) => !/var\(--script-fallbacks(-mono)?\)/.test(s.text))
      .map((s) => `${s.file}:${s.line}: ${s.text.trim().slice(0, 96)}`)
    expect(
      offenders,
      `these declare a font stack without the script fallbacks:\n${offenders.join('\n')}`,
    ).toEqual([])
  })

  it('puts the aliases ahead of every Latin base family', () => {
    const offenders: string[] = []
    for (const site of declarationSites()) {
      const aliasAt = site.text.search(/var\(--script-fallbacks(-mono)?\)/)
      if (aliasAt < 0) continue
      for (const base of BASE_FAMILIES) {
        const baseAt = site.text.indexOf(base)
        if (baseAt >= 0 && baseAt < aliasAt) {
          offenders.push(`${site.file}:${site.line}: ${base} precedes the aliases`)
        }
      }
    }
    expect(offenders, offenders.join('\n')).toEqual([])
  })
})
