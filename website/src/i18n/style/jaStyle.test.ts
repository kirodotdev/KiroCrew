/**
 * Japanese style guards.
 *
 * `catalogParity` proves the catalogs are structurally sound (same keys, no
 * empties, placeholders preserved). It cannot see whether the Japanese *reads*
 * like Japanese — which is how a catalog that is 100% "translated" still ships
 * ASCII commas after kana, `コード レビュー` beside `サブエージェント`, and a
 * だ・である sentence in the middle of です・ます copy.
 *
 * These tests encode the normative rules in `style/ja.md` so that drift fails CI
 * instead of accumulating. Every assertion below corresponds to a numbered rule
 * in that document.
 */

import { describe, it, expect } from 'vitest'

import { CATALOGS as RUNTIME_CATALOGS } from '../index'

/** Kana and kanji. Deliberately excludes the CJK punctuation block. */
const JP = '぀-ゟ゠-ヿ一-鿿々'
const JP_RE = new RegExp(`[${JP}]`)
/** Katakana proper plus the long-vowel mark, which only occurs inside one. */
const KATAKANA = 'ァ-ヴー'

function flatten(obj: unknown, prefix = ''): Record<string, string> {
  const out: Record<string, string> = {}
  if (obj === null || typeof obj !== 'object') return out
  for (const [key, value] of Object.entries(obj as Record<string, unknown>)) {
    const path = prefix ? `${prefix}.${key}` : key
    if (value !== null && typeof value === 'object') {
      Object.assign(out, flatten(value, path))
    } else {
      out[path] = String(value)
    }
  }
  return out
}

const bundle = (code: string) =>
  flatten((RUNTIME_CATALOGS as Record<string, { translation: unknown }>)[code].translation)

const en = bundle('en')
const ja = bundle('ja')

/**
 * Mask runs that are code rather than prose, so the punctuation rules below
 * cannot fire on a file path, a version number, a dotted config key or a URL
 * query string. Mirrors the carve-out list in style/ja.md §1.
 *
 * The mask is NUL, not a space: `インタラクティブ {{provider}}/Kiro グローバル`
 * masked to spaces reads as two adjacent katakana runs and trips the §2 spacing
 * rule for text that never had a space between them.
 */
const MASK = '\u0000'

function stripCode(s: string): string {
  return s
    .replace(/\{\{[^}]*\}\}/g, MASK)
    .replace(/`[^`]*`/g, MASK)
    .replace(/https?:\/\/\S+/g, MASK)
    .replace(/~?\/[\w./~-]+/g, MASK)
    .replace(/\b[\w-]+\.(?:json|ya?ml|md|sh|ts|tsx|py|mjs|png|zip|ics)\b/g, MASK)
    .replace(/\bv?\d+(?:\.\d+)+\b/g, MASK)
    .replace(/\b[\w-]+(?:\.[\w-]+){2,}\b/g, MASK)
    .replace(/\b[a-z]+_[a-z_]+\b/g, MASK)
}

const prose = Object.entries(ja).filter(([, value]) => JP_RE.test(value))

function report(bad: string[], limit = 6): string {
  return `${bad.length} violation(s):\n  ${bad.slice(0, limit).join('\n  ')}`
}

/** Keys whose stripped value trips `re`, formatted for the failure message. */
function offenders(re: RegExp): string[] {
  return prose
    .filter(([, value]) => re.test(stripCode(value)))
    .map(([key, value]) => `${key}: ${JSON.stringify(value.slice(0, 60))}`)
}

describe('ja punctuation (style/ja.md §1)', () => {
  it('uses 、 rather than an ASCII comma after Japanese', () => {
    // An ASCII comma after kana is the single most obvious tell that a string
    // was translated by a tool and never read by a human.
    const bad = offenders(new RegExp(`[${JP}],`))
    expect(bad, report(bad)).toEqual([])
  })

  it('uses the full-width ellipsis for pending states', () => {
    const bad = offenders(new RegExp(`[${JP}]\\.{2,}`))
    expect(bad, report(bad)).toEqual([])
  })

  it('uses full-width ！ and ？ after Japanese', () => {
    // A half-width `?` opening a URL query follows Latin, so stripCode plus the
    // preceding-character requirement keeps this off code.
    const bad = offenders(new RegExp(`[${JP}][!?]`))
    expect(bad, report(bad)).toEqual([])
  })

  it('keeps the em dash the English uses, not an ASCII hyphen', () => {
    // Judged against the English so a genuine hyphen (a Markdown bullet, a
    // hyphenated compound) is never rewritten: only a key whose source already
    // carries — or – is required to carry one in Japanese.
    const bad = prose
      .filter(([key, value]) => {
        const source = en[key] ?? ''
        if (!/[—–]/.test(source)) return false
        return new RegExp(`[${JP}]\\s+-\\s+`).test(stripCode(value))
      })
      .map(([key, value]) => `${key}: ${JSON.stringify(value.slice(0, 60))}`)
    expect(bad, report(bad)).toEqual([])
  })

  it('quotes an interpolated value with 「」, never ASCII or curly quotes', () => {
    // The same trust action rendered `"{{cmd}}"を信頼` on one surface and
    // 「{{cmd}}」を信頼 on another; a user meets both in one session.
    const bad = prose
      .filter(([, value]) => /["“”]\{\{/.test(value) || /\}\}["“”]/.test(value))
      .map(([key, value]) => `${key}: ${JSON.stringify(value.slice(0, 60))}`)
    expect(bad, report(bad)).toEqual([])
  })

  it('never stores an ideographic space', () => {
    // U+3000 is layout smuggled into a string. Spacing is the stylesheet's job,
    // and the character survives trim() so it cannot be normalised downstream.
    const bad = offenders(/　/)
    expect(bad, report(bad)).toEqual([])
  })
})

describe('ja katakana (style/ja.md §2)', () => {
  it('closes up compound loanwords instead of spacing them', () => {
    // `ナレッジ ライブラリ` is the Microsoft-style convention; this catalog uses
    // the closed form, which `サブエージェント` and `ワークフロー` already set.
    // A split run also breaks line-breaking: JLReq lets a line break at the
    // space, orphaning the tail of one word.
    const bad = offenders(new RegExp(`[${KATAKANA}][ 　]+[${KATAKANA}]`))
    expect(bad, report(bad)).toEqual([])
  })

  it('keeps the trailing long-vowel mark on absorbed loanwords', () => {
    // JTF keeps it (`フォルダー`); the older JIS convention truncated it
    // (`フォルダ`). Mixing both renders the same product noun two ways.
    const bad = offenders(/(ブラウザ|フォルダ|コンピュータ|サーバ|ユーザ|メンバ)(?!ー)/)
    expect(bad, report(bad)).toEqual([])
  })
})

describe('ja terminology (style/ja.md §2.1)', () => {
  // One concept, one word. Each entry is [english cue, banned rendering,
  // canonical rendering]; the cue keeps the check context-sensitive, so a
  // banned string is only a violation where the English proves the sense —
  // `ライト` in "light theme" is untouched.
  const BANNED: Array<[string, string, string]> = [
    ['write access', 'ライトアクセス', '書き込みアクセス'],
    ['present', 'プレゼント', '在席'],
  ]

  for (const [cue, banned, canonical] of BANNED) {
    it(`renders '${cue}' as ${canonical}, never ${banned}`, () => {
      const bad = Object.keys(ja).filter(
        k => (en[k] ?? '').includes(cue) && ja[k].includes(banned),
      )
      expect(bad, report(bad)).toEqual([])
    })
  }
})

describe('ja register (style/ja.md §4)', () => {
  it('never drops into だ・である in UI copy', () => {
    // 体言止め — a bare noun phrase closed with 。 — is the standard form for a
    // UI description and is NOT a violation, so this matches verb endings only.
    const bad = offenders(/(である|(?<![まで])した|(?<!ま)する|(?<![んでま])だ)。/)
    expect(bad, report(bad)).toEqual([])
  })

  it('writes auxiliaries in kana, not kanji', () => {
    const bad = offenders(/(下さい|出来る|出来ま|居ま|有りま|無い事)/)
    expect(bad, report(bad)).toEqual([])
  })

  it('does not end a button or menu label with 。', () => {
    // A label is 体言止め with no full stop. Only keys whose ENGLISH is a bare
    // label (no terminal punctuation, at most three words) are judged, so a
    // genuine sentence is never caught.
    const bad = prose
      .filter(([key, value]) => {
        const source = en[key]
        if (source === undefined || /[.!?:…]$/.test(source)) return false
        return source.trim().split(/\s+/).length <= 3 && value.endsWith('。')
      })
      .map(([key, value]) => `${key}: ${JSON.stringify(value)}`)
    expect(bad, report(bad)).toEqual([])
  })
})

describe('ja plural forms (style/ja.md §5)', () => {
  it('never supplies a plural category Japanese does not have', () => {
    // Japanese has exactly one CLDR plural category: `other`. A `_one` key is
    // therefore unreachable — and worse, it makes the catalog look like it
    // handles counting when it silently cannot.
    // A key may end in `_one` simply because its English sentence ends with the
    // WORD "one"; only a key with an `_other` sibling is a real plural family.
    const keys = new Set(Object.keys(ja))
    const bad = [...keys].filter(k => {
      const m = k.match(/^(.*)_(one|two|few|many)$/)
      return m !== null && keys.has(`${m[1]}_other`)
    })
    expect(bad, report(bad)).toEqual([])
  })
})
