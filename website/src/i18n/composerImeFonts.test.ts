/**
 * The rich-text composer's font stacks.
 *
 * While an IME composition is open, Lexical keeps a U+200B after the composed text.
 * When the run holding it is shaped by a script alias face (font-faces.css), which has
 * no space glyph because its unicode-range claims no U+0020, Chromium merges U+200B
 * into the kana or Hangul before it and draws the caret halfway through that glyph.
 * The fix lists the bundled Latin face ahead of the aliases in the Lexical editor
 * only. That is a no-op for every painting character only while the properties
 * pinned here hold:
 *
 *  (a) each composer stack IS the global stack with exactly one family moved, so a
 *      later edit to --font-body / --mono / FAMILY_MAP cannot silently drift from it;
 *  (b) the rules apply only where those stacks are what the editor would draw with
 *      (the data-font-family values, not OpenDyslexic's --mono), behind the same
 *      --theme-font-* guard a theme pack fills;
 *  (c) no Latin face subset claims a code point any alias claims, so moving the Latin
 *      face forward changes the face of no character that paints. A default-ignorable
 *      the Latin subset claims does move to it: U+200B, which is the fix, and U+200C,
 *      which only shifts where a caret lands between a virama and its consonant
 *      (measured pixel-identical, so (c) is not a proof that nothing moved);
 *  (d) the Latin faces claim U+0020 and U+200B, which is what makes U+200B a
 *      zero-width cluster of its own again.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, it, expect } from 'vitest'
import { readIndexCss } from '../test/indexCssSource'
import { INLINE_MARKDOWN_THEME } from '../components/InlineMarkdownPlugin'

const SRC = join(__dirname, '..')
const read = (rel: string): string => readFileSync(join(SRC, rel), 'utf8')
const INDEX_CSS = readIndexCss()
const TOKENS_CSS = read('styles/design-tokens.css')
const TRANSCRIPT_CSS = read('styles/chat-transcript.css')
const USE_ZOOM = read('hooks/useZoom.ts')

const SANS_SELECTORS = [':is(html:not([data-font-family]),html[data-font-family="sans"]) [data-lexical-composer]']
const MONO_SELECTORS = [
  'html[data-font-family="mono"] [data-lexical-composer]',
  'html:not([data-font-family="opendyslexic"]) [data-lexical-composer] .font-mono',
]

/** Split a CSS value on its top-level commas (commas inside `var(...)` stay). */
function splitTopLevel(value: string): string[] {
  const out: string[] = []
  let depth = 0
  let cur = ''
  for (const ch of value) {
    if (ch === '(') depth++
    if (ch === ')') depth--
    if (ch === ',' && depth === 0) {
      out.push(cur.trim())
      cur = ''
    } else {
      cur += ch
    }
  }
  if (cur.trim()) out.push(cur.trim())
  return out
}

/** `var(--theme-font-x, <fallback>)` → its role token and the fallback's families. */
function themeGuarded(value: string): { token: string; families: string[] } {
  const m = value.trim().match(/^var\((--theme-font-(?:sans|mono)),([\s\S]*)\)$/)
  if (!m) throw new Error(`not a --theme-font-* guarded stack: ${value}`)
  return { token: m[1], families: splitTopLevel(m[2]) }
}

function tokenValue(block: string, name: string): string {
  const m = block.match(new RegExp(`(?:^|[;{\\s])${name}:\\s*([^;]+);`))
  if (!m) throw new Error(`no ${name} in block`)
  return m[1].trim()
}

const ROOT_BLOCK = TOKENS_CSS.match(/:root\s*\{([^}]*)\}/)?.[1] ?? ''
/** Every block that sets the script tokens: :root plus one per html:lang() rule. */
const SCRIPT_TOKEN_BLOCKS = [...TOKENS_CSS.matchAll(/([^{}]+)\{([^}]*--script-fallbacks:[^}]*)\}/g)].map(m => ({
  label: m[1].trim().split('\n').pop()?.trim() ?? '',
  body: m[2],
}))

/** Replace `var(--script-fallbacks[-mono])` with the families one locale block sets. */
function expand(families: string[], block: string): string[] {
  const out: string[] = []
  for (const f of families) {
    const v = f.match(/^var\((--script-fallbacks(?:-mono)?)\)$/)
    for (const name of v ? splitTopLevel(tokenValue(block, v[1])) : [f]) {
      if (!out.includes(name)) out.push(name)
    }
  }
  return out
}

/** The composer rule whose selector list is exactly `selectors`. */
function composerRule(selectors: string[]): { selectors: string[]; value: string } | null {
  for (const m of INDEX_CSS.matchAll(/(?<=^|\})\s*([^{}@]*\[data-lexical-composer\][^{}]*)\{\s*font-family:([^;}]+);?\s*\}/g)) {
    const found = m[1].replace(/\/\*[\s\S]*?\*\//g, '').trim().split(/,(?![^(]*\))/).map(s => s.trim())
    if (found.join('|') === selectors.join('|')) return { selectors: found, value: m[2].trim() }
  }
  return null
}

function familyMapEntry(key: 'sans' | 'mono'): string {
  const m = USE_ZOOM.match(new RegExp(`^\\s*${key}:\\s*"([^"]+)"`, 'm'))
  if (!m) throw new Error(`FAMILY_MAP.${key} not found`)
  return m[1]
}

function moved(list: string[], family: string, at: number): string[] {
  const rest = list.filter(f => f !== family)
  return [...rest.slice(0, at), family, ...rest.slice(at)]
}

type Range = { lo: number; hi: number }

/** Every @font-face block, as its family and its own unicode-range. */
const FACES = [...INDEX_CSS.matchAll(/@font-face\s*\{([^}]*)\}/g)].map(m => {
  const family = m[1].match(/font-family:\s*'([^']+)'/)?.[1] ?? ''
  const decl = m[1].match(/unicode-range:\s*([^;}]+)/)
  const ranges: Range[] = decl
    ? decl[1].split(',').map(s => s.trim()).filter(Boolean).map(token => {
        const r = token.match(/^U\+([0-9A-Fa-f]+)(?:-([0-9A-Fa-f]+))?$/)
        if (!r) throw new Error(`unparseable unicode-range token: "${token}"`)
        const lo = parseInt(r[1], 16)
        return { lo, hi: r[2] ? parseInt(r[2], 16) : lo }
      })
    : []
  return { family, ranges }
})
const LATIN_FACES = ['Space Grotesk', 'JetBrains Mono']
const hex = (n: number) => `U+${n.toString(16).toUpperCase().padStart(4, '0')}`

describe('rich-text composer font stacks', () => {
  const sans = composerRule(SANS_SELECTORS)
  const mono = composerRule(MONO_SELECTORS)

  it('declares the sans and mono composer rules in chat-transcript.css', () => {
    expect(sans, 'no sans [data-lexical-composer] font-family rule').not.toBeNull()
    expect(mono, 'no mono [data-lexical-composer] font-family rule').not.toBeNull()
    expect(TRANSCRIPT_CSS).toContain('[data-lexical-composer]{font-family:')
  })

  it('targets the class the inline-code format renders with', () => {
    expect(INLINE_MARKDOWN_THEME.text?.code?.split(/\s+/)).toContain('font-mono')
  })

  it('reads the same theme role token as the global stacks', () => {
    expect(themeGuarded(sans!.value).token).toBe('--theme-font-sans')
    expect(themeGuarded(mono!.value).token).toBe('--theme-font-mono')
  })

  it('equals FAMILY_MAP.sans and --font-body with only Space Grotesk moved after KC Straight Quotes', () => {
    const composer = themeGuarded(sans!.value).families
    expect(composer[0], "'KC Straight Quotes' must stay first").toBe("'KC Straight Quotes'")
    for (const source of [familyMapEntry('sans'), tokenValue(ROOT_BLOCK, '--font-body')]) {
      const global = themeGuarded(source)
      expect(global.token).toBe('--theme-font-sans')
      expect(SCRIPT_TOKEN_BLOCKS.length).toBeGreaterThanOrEqual(4)
      for (const { label, body } of SCRIPT_TOKEN_BLOCKS) {
        const want = moved(expand(global.families, body), "'Space Grotesk'", 1)
        expect(expand(composer, body), label).toEqual(want)
      }
    }
  })

  it('equals FAMILY_MAP.mono and --mono with only JetBrains Mono moved first', () => {
    const composer = themeGuarded(mono!.value).families
    for (const source of [familyMapEntry('mono'), tokenValue(ROOT_BLOCK, '--mono')]) {
      const global = themeGuarded(source)
      expect(global.token).toBe('--theme-font-mono')
      for (const { label, body } of SCRIPT_TOKEN_BLOCKS) {
        const want = moved(expand(global.families, body), "'JetBrains Mono'", 0)
        expect(expand(composer, body), label).toEqual(want)
      }
    }
  })

  it.each(LATIN_FACES.flatMap(family =>
    FACES.filter(f => f.family === family).map((f, i) => ({ name: `${family} #${i + 1}`, ranges: f.ranges })),
  ))('$name claims no code point any script alias claims', ({ ranges }) => {
    // 'KC Straight Quotes' is not a script alias: it stays FIRST in the composer
    // stack, so its order relative to the Latin face does not change.
    const aliases = FACES.filter(f => /^KC .* Fallback$/.test(f.family))
    expect(aliases.length).toBeGreaterThan(10)
    expect(ranges.length).toBeGreaterThan(0)
    const hits: string[] = []
    for (const r of ranges) {
      for (const a of aliases) {
        for (const q of a.ranges) {
          if (r.lo <= q.hi && q.lo <= r.hi) hits.push(`${hex(r.lo)}-${hex(r.hi)} vs '${a.family}' ${hex(q.lo)}-${hex(q.hi)}`)
        }
      }
    }
    expect(hits, hits.join('\n')).toEqual([])
  })

  it.each(LATIN_FACES)('%s claims U+0020 and U+200B', (family) => {
    const ranges = FACES.filter(f => f.family === family).flatMap(f => f.ranges)
    for (const cp of [0x20, 0x200b]) {
      expect(ranges.some(r => r.lo <= cp && cp <= r.hi), `${family} does not claim ${hex(cp)}`).toBe(true)
    }
  })
})
