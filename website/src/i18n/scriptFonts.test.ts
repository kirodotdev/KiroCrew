/**
 * Script fallback faces must lead every font stack, and must never claim Latin.
 *
 * zh-CN, ja, ko, hi and bn have no font coverage otherwise: every family in
 * `--font-body` and `--mono` covers Latin only, so those scripts fall through to the
 * browser's per-script fallback, which picks a face per character and silently
 * mismatches. CJK punctuation is the visible symptom — Unicode uses one code point
 * for the Chinese and Japanese comma and full stop, so only the font decides where
 * in the em box the glyph sits.
 *
 * The mechanism is eight `unicode-range`-restricted `@font-face` aliases over
 * locally installed faces, placed at the FRONT of each stack. Three properties
 * make that correct, and all are asserted here because none is visible from
 * reading a family list:
 *
 *  1. **No alias range may include Latin.** `unicode-range` is what makes a leading
 *     position safe: a face whose range excludes Latin is never consulted for
 *     Latin, so leading cannot change how Latin renders. If someone widens a range
 *     to cover Latin, every stack in the app silently switches its Latin face.
 *     This is the assertion that matters most.
 *  2. **Every declaration site must reference the alias token.** The stacks are
 *     declared across `index.css`, `hooks/themeCss.ts` (the built-in defaults plus
 *     the `--theme-font-sans` / `--theme-font-mono` role tokens an installed pack
 *     fills) and the three `FAMILY_MAP` entries in `hooks/useZoom.ts` that are
 *     written into `--font-body` at runtime. One added later without the aliases
 *     would silently lose script coverage on whichever path it feeds, so the check
 *     globs the tree rather than naming files.
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

import { readFileSync } from 'node:fs'
import { readFile, readdir } from 'node:fs/promises'
import { basename, join, relative } from 'node:path'

import { describe, it, expect } from 'vitest'

const SRC = join(__dirname, '..')
const INDEX_CSS = readFileSync(join(SRC, 'index.css'), 'utf8')

/** Region-specific Han aliases must never share an active token. */
const SC_ALIASES = [
  'KC Han Fallback',
  'KC Han Mono Fallback',
] as const

/**
 * Locale-specific aliases, keyed by the `html:lang()` rule that activates them.
 *
 * These are NOT the untagged default. A leading Simplified Chinese face would
 * draw Japanese and Korean content (and untagged CJK in an English UI) with
 * the wrong regional glyph forms. Each entry REPLACES every other Han pair
 * rather than prepending to it.
 */
const REGIONAL = [
  { lang: 'zh-CN', body: 'KC Han Fallback', mono: 'KC Han Mono Fallback' },
  { lang: 'ja', body: 'KC Japanese Fallback', mono: 'KC Japanese Mono Fallback' },
  { lang: 'ko', body: 'KC Korean Fallback', mono: 'KC Korean Mono Fallback' },
] as const

const REGIONAL_ALIASES = REGIONAL.flatMap(r => [r.body, r.mono])

/**
 * A code point only this locale's aliases must cover. Kana for Japanese, Hangul
 * for Korean — the scripts that are absent from the other's faces, so a swapped
 * or merged token fails here rather than rendering from the OS cascade.
 */
const SCRIPT_PROBES: Record<string, ReadonlyArray<readonly [string, number]>> = {
  'zh-CN': [['CJK Unified Ideographs', 0x4e00], ['CJK punctuation', 0x3001]],
  ja: [['hiragana', 0x3042], ['katakana', 0x30a2]],
  ko: [['Hangul syllables', 0xac00], ['Hangul compatibility jamo', 0x3131]],
}

/** Script aliases shared by every locale. */
const COMMON_ALIASES = [
  'KC Devanagari Fallback',
  'KC Bengali Fallback',
] as const

const ALIASES = [...SC_ALIASES, ...REGIONAL_ALIASES, ...COMMON_ALIASES] as const

/**
 * Ranges that must stay OUT of every alias. Latin proper plus general punctuation:
 * an alias claiming U+2000-206F would take over quotes, dashes and ellipses in
 * Latin text, which is the same class of silent regression as claiming Latin.
 *
 * The ONE intentional exception is 'KC Straight Quotes' (#6374): it claims exactly
 * U+0022 and U+0027 to replace Space Grotesk's miscut straight quotes. It is
 * deliberately NOT in ALIASES, so the checks above never run on it; instead it is
 * pinned separately at the bottom of this file to those two code points and nothing
 * wider, which is what keeps the exception from quietly growing into a Latin claim.
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

function covers(family: string, codePoint: number): boolean {
  return parseUnicodeRange(faceBlock(family)).some(r => r.lo <= codePoint && codePoint <= r.hi)
}

function ruleBody(pattern: RegExp): string {
  return INDEX_CSS.match(pattern)?.[1] ?? ''
}

/** An `html:lang(tag)` rule. Document-level only — no descendant content scope. */
function htmlLangRuleBody(lang: string): string {
  return ruleBody(new RegExp(`html:lang\\(${lang}\\)\\s*\\{([^}]*)\\}`))
}

/** A token is unusable unless it is present and carries the shared script aliases. */
function expectCommon(label: string, value: string): void {
  expect(value, `no script fallback token for ${label}`).not.toBe('')
  for (const family of COMMON_ALIASES) {
    expect(value, `${family} missing from ${label}`).toContain(family)
  }
}

/**
 * The proportional token must NOT carry the mono alias; the mono token must carry
 * both, mono first. That order is what makes a code block fall back to the
 * proportional face only when the monospace one is not installed — the pairing the
 * browser's own fallback produces anyway, and better than a missing glyph.
 */
function expectAliasPair(
  label: string,
  body: string,
  mono: string,
  proportional: string,
  monospace: string,
): void {
  expect(body, `${proportional} missing from the ${label} body token`).toContain(proportional)
  expect(body, `${monospace} must not appear in the ${label} body token`).not.toContain(monospace)
  expect(mono, `${monospace} missing from the ${label} mono token`).toContain(monospace)
  expect(mono, `${proportional} missing from the ${label} mono token`).toContain(proportional)
  expect(mono.indexOf(monospace), `${label} mono token must lead with ${monospace}`)
    .toBeLessThan(mono.indexOf(proportional))
}

function scriptToken(block: string, mono = false): string {
  const pattern = mono
    ? /--script-fallbacks-mono:\s*([^;]+);/
    : /--script-fallbacks:\s*([^;]+);/
  return block.match(pattern)?.[1] ?? ''
}

/** Every file that DECLARES --font-body or --mono, found by walking the tree. */
async function declarationSites(): Promise<Array<{ file: string; line: number; text: string }>> {
  const out: Array<{ file: string; line: number; text: string }> = []
  const sourceFiles = async (dir: string): Promise<string[]> => {
    const entries = await readdir(dir, { withFileTypes: true })
    const nested = await Promise.all(entries.map(async (entry) => {
      if (entry.name === 'node_modules' || entry.name.startsWith('.')) return []
      const full = join(dir, entry.name)
      if (entry.isDirectory()) return sourceFiles(full)
      return /\.(css|ts|tsx)$/.test(entry.name) ? [full] : []
    }))
    return nested.flat()
  }

  const files = await sourceFiles(SRC)
  let next = 0
  const scan = async () => {
    while (next < files.length) {
      const full = files[next++]
      const entry = basename(full)
      // Tests never declare a font stack, and this file necessarily contains the
      // detection pattern as a literal — without the exclusion it matches itself.
      // The only false-negative this creates is a stack declared inside a test,
      // which would not reach the app.
      if (/\.test\.(ts|tsx)$/.test(entry)) continue
      const content = await readFile(full, 'utf8')
      content
        .split('\n')
        .forEach((text, i) => {
          // A declaration, not a read: `--font-body:` / `--mono:` / a role token
          // with a value, and the FAMILY_MAP entries that are written into
          // --font-body at runtime. The role tokens count because a pack's stack
          // is built from them, so one declared without the aliases loses script
          // coverage for every user of that pack.
          const declares = /--(?:font-body|mono|theme-font-sans|theme-font-mono)\s*:/.test(text)
          const familyMap = /^\s*(?:sans|mono|system):\s*"/.test(text)
          if (declares || familyMap) out.push({ file: relative(SRC, full), line: i + 1, text })
        })
    }
  }
  // Files are independent. A bounded worker set avoids both the serial OneDrive
  // walk that exceeded Vitest's test budget under full-suite load and an
  // unbounded Promise.all that could exhaust file descriptors on a larger tree.
  await Promise.all(Array.from({ length: Math.min(16, files.length) }, scan))
  return out.sort((a, b) => a.file.localeCompare(b.file) || a.line - b.line)
}

// The three assertions inspect the same immutable source snapshot. Scan once so
// later assertions cannot pay another full-tree I/O pass or observe a different
// tree halfway through the file.
const allDeclarationSites = declarationSites()

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

  it.each(REGIONAL)('covers the $lang script in both of its aliases', ({ lang, body, mono }) => {
    for (const [script, codePoint] of SCRIPT_PROBES[lang]) {
      for (const family of [body, mono]) {
        expect(covers(family, codePoint), `'${family}' does not cover ${script}`).toBe(true)
      }
    }
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

  it('keeps regional Han aliases out of the untagged default token', () => {
    const rootBlock = ruleBody(/:root\s*\{([^}]*)\}/)
    expect(rootBlock, 'no :root block found in index.css').not.toBe('')

    const root = scriptToken(rootBlock)
    const rootMono = scriptToken(rootBlock, true)
    expectCommon('root body', root)
    expectCommon('root mono', rootMono)
    // Untagged CJK (English UI, Japanese chat, mixed messages) must reach the
    // browser/OS locale-aware cascade. A leading regional Han alias would force
    // every shared ideograph through one region's glyph forms.
    for (const family of REGIONAL_ALIASES) {
      expect(root, `${family} leaked into the default body token`).not.toContain(family)
      expect(rootMono, `${family} leaked into the default mono token`).not.toContain(family)
    }
  })

  it.each(REGIONAL)('swaps to isolated aliases for html:lang($lang)', ({ lang, body, mono }) => {
    const block = htmlLangRuleBody(lang)
    expect(block, `no html:lang(${lang}) block found in index.css`).not.toBe('')

    const bodyToken = scriptToken(block)
    const monoToken = scriptToken(block, true)
    expectCommon(`${lang} body`, bodyToken)
    expectCommon(`${lang} mono`, monoToken)
    expectAliasPair(lang, bodyToken, monoToken, body, mono)

    // Every alias that is not this locale's own must be ABSENT, not merely later:
    // a retained Simplified face would sit in front of the one face that can draw
    // this locale's script, and the browser's lang-aware fallback is never reached.
    const foreign = [...SC_ALIASES, ...REGIONAL_ALIASES].filter(f => f !== body && f !== mono)
    for (const family of foreign) {
      expect(bodyToken, `${family} leaked into the ${lang} body token`).not.toContain(family)
      expect(monoToken, `${family} leaked into the ${lang} mono token`).not.toContain(family)
    }
  })

  it('scopes Simplified aliases to html:lang(zh-CN), not a bare :lang(zh)', () => {
    // `:lang(zh)` also matches zh-TW / zh-HK / zh-Hant and would force
    // Traditional content through Simplified faces — the same class of bug
    // this change removes for Japanese. The dashboard's Chinese UI is zh-CN.
    const bareZh = INDEX_CSS.match(/(?<![\w-]):lang\(zh\)(?=\s*[,{])/)
    expect(bareZh, 'bare :lang(zh) matches Traditional Chinese tags').toBeNull()
    expect(INDEX_CSS).toMatch(/html:lang\(zh-CN\)/)
    expect(INDEX_CSS).not.toMatch(/:lang\(zh-Hans\)/)
  })
})

describe('font stack declarations', () => {
  it('finds every declaration site the tree actually contains', async () => {
    // Guards the walker itself: if this drops to a handful, the glob broke and the
    // next assertion would pass vacuously.
    expect((await allDeclarationSites).length).toBeGreaterThanOrEqual(12)
  })

  it('references the alias token at every declaration site', async () => {
    const offenders = (await allDeclarationSites)
      .filter((s) => !/var\(--script-fallbacks(-mono)?\)/.test(s.text))
      .map((s) => `${s.file}:${s.line}: ${s.text.trim().slice(0, 96)}`)
    expect(
      offenders,
      `these declare a font stack without the script fallbacks:\n${offenders.join('\n')}`,
    ).toEqual([])
  })

  it('puts the aliases ahead of every Latin base family', async () => {
    const offenders: string[] = []
    for (const site of await allDeclarationSites) {
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

/**
 * The straight-quote alias is the deliberate inverse of every alias above: those
 * exist to add script coverage WITHOUT touching Latin, while this one exists to
 * REPLACE the Latin body face for exactly two code points. Space Grotesk (the
 * default body face) draws U+0022 and U+0027 as its closing curly glyph, so a
 * straight quote in UI copy — or one a user types — renders as ” / ’ on the wrong
 * side of the word (#6374). The alias leads the sans stack with a local()
 * straight-quote face and a unicode-range of just those two code points.
 *
 * Two properties keep it safe, and both are pinned here because neither is visible
 * from reading a family list: it may claim ONLY U+0022 and U+0027 (widening it back
 * into Latin is the regression FORBIDDEN guards for the other aliases), and it must
 * stay OUT of the mono tokens (JetBrains Mono already draws straight quotes, so the
 * override is neither needed nor wanted there).
 */
describe('KC Straight Quotes (#6374) — the one deliberate Latin-claiming alias', () => {
  const FAMILY = 'KC Straight Quotes'

  it('exists, is local()-only, and claims EXACTLY U+0022 and U+0027', () => {
    const block = faceBlock(FAMILY)
    expect(block, `no @font-face for '${FAMILY}' in index.css`).not.toBe('')
    expect(block).toMatch(/src:[^;]*local\(/)
    expect(block, `'${FAMILY}' must not fetch a remote font`).not.toMatch(/url\(/)
    const ranges = parseUnicodeRange(block).map((r) => [r.lo, r.hi]).sort((a, b) => a[0] - b[0])
    // The whole safety of a Latin-claiming leading alias is that it claims these
    // two code points and no others. Any wider range is a silent Latin takeover.
    expect(ranges).toEqual([[0x22, 0x22], [0x27, 0x27]])
  })

  it('leads every sans --script-fallbacks and appears in none of the mono ones', () => {
    const sansSites: Array<[string, string]> = [
      ['root', ruleBody(/:root\s*\{([^}]*)\}/)],
      ...REGIONAL.map(({ lang }) => [lang, htmlLangRuleBody(lang)] as [string, string]),
    ]
    for (const [label, block] of sansSites) {
      const sans = scriptToken(block)
      const mono = scriptToken(block, true)
      expect(sans, `'${FAMILY}' missing from the ${label} sans token`).toContain(FAMILY)
      // Leading is what makes it win for U+0022/U+0027 over the body face without a
      // unicode-range collision changing anything else (see the aliases' rationale).
      expect(sans.trimStart().startsWith(`'${FAMILY}'`), `'${FAMILY}' must lead the ${label} sans token`).toBe(true)
      expect(mono, `'${FAMILY}' must not appear in the ${label} mono token`).not.toContain(FAMILY)
    }
  })
})
