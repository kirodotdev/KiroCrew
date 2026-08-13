/**
 * Korean style guards.
 *
 * `catalogParity` proves the catalogs are structurally sound (same keys, no
 * empties, placeholders preserved). It cannot see whether the Korean *reads* like
 * Korean — which is how a catalog that is 100% "translated" still ships
 * full-width `。` after Hangul, `메세지` beside `메시지`, a bare `를` welded to an
 * interpolation, and a 한다체 sentence in the middle of 합니다체 copy.
 *
 * These tests encode the normative rules in `style/ko.md` so that drift fails CI
 * instead of accumulating. Every assertion below corresponds to a numbered rule
 * in that document.
 */

import { describe, it, expect } from 'vitest'

import { CATALOGS as RUNTIME_CATALOGS } from '../index'

const HANGUL = '가-힣'
const HANGUL_RE = new RegExp(`[${HANGUL}]`)
/** Kana and kanji — neither belongs in Korean copy. Excludes CJK punctuation. */
const CJK = '぀-ゟ゠-ヿ一-鿿々'

/**
 * 조사 and 접미사 that may attach directly to a Latin run (`Slack에서`, `GitHub을`,
 * `Kiro별`). Everything else following a Latin run is a noun and takes a space.
 *
 * The boundary check compares the WHOLE Hangul run after the Latin against this
 * set, not a prefix of it, so `서버를` cannot pass by matching its `를` tail.
 */
const PARTICLES = new Set([
  '은', '는', '이', '가', '을', '를', '의', '와', '과', '도', '만', '에', '에서',
  '에게', '에게서', '에도', '에는', '에만', '에서는', '에서만', '에서의', '으로',
  '로', '으로써', '로써', '으로서', '로서', '으로는', '로는', '으로만', '로만',
  '부터', '부터의', '까지', '까지의', '보다', '처럼', '같이', '만큼', '조차',
  '마저', '밖에', '뿐', '마다', '대로', '씩', '이나', '나', '이란', '란',
  '이라는', '라는', '이라고', '라고', '이라도', '라도', '이거나', '거나', '이든',
  '든', '이랑', '랑', '와의', '과의', '만을', '만이', '이며', '며', '에서도',
  '으로도', '로도', '까지도', '부터도', '이므로', '므로', '으로부터', '로부터',
  '입니다', '이었습니다', '였습니다',
])

/**
 * A 조사 or 서술격조사 riding on a 접미사 or 단위명사: `N개를`, `Transcribe용을`,
 * `N건입니다`. Stripped before the single-syllable allowance below, so the
 * two-token case is judged on the token that actually decides the spacing.
 */
const TRAILING_PARTICLE = /(?:이었습니다|였습니다|입니다|입니까|이고|이며|이나|이란|은|는|이|가|을|를|의|와|과|도|만|에)$/

/**
 * 하다/되다 turn a Latin noun into a Korean verb and attach with no space:
 * `POST할 수 있습니다`, `commit되었습니다`. The conjugation changes the syllable
 * itself (하/한/할/했/해), so this is a syllable class rather than a stem match.
 */
const VERBALIZER = /^[하한할함합했해되된될됨됩됐돼]/

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
const ko = bundle('ko')

/**
 * Mask runs that are code rather than prose, so the punctuation and spacing rules
 * below cannot fire on a file path, a version number, a dotted config key or a URL
 * query string. Mirrors the carve-out list in style/ko.md §1.
 *
 * The mask is NUL, not a space: masking `{{provider}}서버` to a space would invent
 * the very Latin/Hangul boundary space §2 is checking for.
 */
const MASK = String.fromCharCode(0)

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

const prose = Object.entries(ko).filter(([, value]) => HANGUL_RE.test(value))

function report(bad: string[], limit = 6): string {
  return `${bad.length} violation(s):\n  ${bad.slice(0, limit).join('\n  ')}`
}

function describeKey(key: string, value: string): string {
  return `${key}: ${JSON.stringify(value.slice(0, 60))}`
}

/** Keys whose stripped value trips `re`, formatted for the failure message. */
function offenders(re: RegExp): string[] {
  return prose
    .filter(([, value]) => re.test(stripCode(value)))
    .map(([key, value]) => describeKey(key, value))
}

/** Keys whose RAW value trips `re` — for rules about placeholders and quotes. */
function rawOffenders(re: RegExp): string[] {
  return prose
    .filter(([, value]) => re.test(value))
    .map(([key, value]) => describeKey(key, value))
}

describe('ko punctuation (style/ko.md §1)', () => {
  it('uses half-width punctuation, never the full-width CJK forms', () => {
    // Korean orthography prescribes ASCII `.` and `,` even between Hangul. A
    // full-width form here means the value was carried over from ja.json or
    // zh-CN.json rather than translated.
    const bad = offenders(/[。，、！？；：（）「」『』【】〈〉《》～]/)
    expect(bad, report(bad)).toEqual([])
  })

  it('uses … for pending states, never an ASCII ellipsis', () => {
    const bad = offenders(new RegExp(`[${HANGUL}]\\.{2,}`))
    expect(bad, report(bad)).toEqual([])
  })

  it('keeps the em dash the English uses, not an ASCII hyphen', () => {
    // Judged against the English so a genuine hyphen (a Markdown bullet, a
    // hyphenated compound) is never rewritten: only a key whose source already
    // carries — or – is required to carry one in Korean.
    const bad = prose
      .filter(([key, value]) => {
        const source = en[key] ?? ''
        if (!/[—–]/.test(source)) return false
        return new RegExp(`[${HANGUL}]\\s+-\\s+`).test(stripCode(value))
      })
      .map(([key, value]) => describeKey(key, value))
    expect(bad, report(bad)).toEqual([])
  })

  it('quotes an interpolated value with ‘ ’, never ASCII or double quotes', () => {
    // The same trust action rendered `"{{cmd}}" 신뢰` on one surface and
    // ‘{{cmd}}’ 신뢰 on another; a user meets both in one session.
    const bad = rawOffenders(/["“”]\{\{|\}\}["“”]/)
    expect(bad, report(bad)).toEqual([])
  })

  it('never stores an ideographic space', () => {
    // U+3000 is layout smuggled into a string. Spacing is the stylesheet's job,
    // and the character survives trim() so it cannot be normalised downstream.
    const bad = offenders(/　/)
    expect(bad, report(bad)).toEqual([])
  })
})

describe('ko script (style/ko.md §1.1)', () => {
  it('contains no 한자 and no kana', () => {
    // The Korean catalog is translated FROM the Japanese one, so a leaked source
    // value is the pipeline's most likely failure. Hangul-only makes that leak a
    // red test rather than shipped Japanese.
    //
    // Relative to the English: a key whose source itself names a CJK string is
    // not the translator's doing.
    const cjk = new RegExp(`[${CJK}]`)
    const bad = Object.entries(ko)
      .filter(([key, value]) => cjk.test(value) && !cjk.test(en[key] ?? ''))
      .map(([key, value]) => describeKey(key, value))
    expect(bad, report(bad)).toEqual([])
  })
})

describe('ko spacing (style/ko.md §2)', () => {
  it('spaces 의존명사 away from the verb it follows', () => {
    // `할수 있습니다` is the single most common Korean spacing error a machine
    // produces. Restricted to the unambiguous families: a 의존명사 rule in general
    // needs a parser, and an approximation would fire on correct copy.
    const bad = offenders(/(?:할|될|볼|줄|올|쓸|갈|열|있을|없을)수|수(?:있|없)|(?:하는|한|할|된|되는)것|(?:하기|이기)때문/)
    expect(bad, report(bad)).toEqual([])
  })

  it('spaces a Latin run away from the noun that follows it', () => {
    // `MCP서버` must be `MCP 서버`, but a 조사 attaches with no space
    // (`Slack에서`) and so does a 하다/되다 conjugation (`POST할`). The whole Hangul
    // run after the Latin is compared against the 조사 set, so `서버를` cannot pass
    // by matching its `를` tail.
    //
    // A SINGLE syllable is always allowed. That is where 접미사 and 단위명사 live —
    // `Transcribe용`, `N개`, `Kiro별` — and all of them bind directly. Enumerating
    // them is endless and every miss blocks correct copy, so the gate trades that
    // recall for precision and keeps the defect it exists for: a multi-syllable
    // noun welded onto Latin.
    const bad = prose
      .filter(([, value]) => {
        for (const m of stripCode(value).matchAll(new RegExp(`[A-Za-z]([${HANGUL}]+)`, 'g'))) {
          const run = m[1]
          if (PARTICLES.has(run) || VERBALIZER.test(run)) continue
          if (run.replace(TRAILING_PARTICLE, '').length > 1) return true
        }
        return false
      })
      .map(([key, value]) => describeKey(key, value))
    expect(bad, report(bad)).toEqual([])
  })

  it('does not open a fragment with a detached 조사', () => {
    // A fragment is rendered as `<operand> <fragment>` — the component always puts
    // a space between them — so a value that BEGINS with a 조사 draws it separated
    // from the noun it belongs to: `Kiro Crew 을(를) 제거하고`. In Korean that is an
    // orthography error, not a style preference, and the space is not the
    // catalog's to remove. Omitting the particle is the repair the language allows.
    //
    // Judged from the English: a value whose source opens lowercase is a fragment
    // continuing a sentence the component started.
    //
    // `이` is both the subject particle and the determiner "this", and only the
    // determiner is followed by its own noun (`이 항목을…`). The particle sense is
    // always written `이(가)` here — §2.1 requires both forms after an operand — so
    // a bare `이 ` at the head is the determiner every time.
    const LEADING = new RegExp(
      `^(을\\(를\\)|이\\(가\\)|은\\(는\\)|와\\(과\\)|\\(으\\)로|을|를|이|가|은|는|와|과|로|의|에서|에)([ ,.:]|$)`,
    )
    const DETERMINER = new RegExp(`^이 [${HANGUL}]`)
    const bad = prose
      .filter(([key, value]) => /^[a-z]/.test(en[key] ?? '')
        && LEADING.test(value) && !DETERMINER.test(value))
      .map(([key, value]) => describeKey(key, value))
    expect(bad, report(bad)).toEqual([])
  })

  it('writes a placeholder 조사 in both forms', () => {
    // The 조사 depends on the final consonant of the preceding syllable, which an
    // interpolation does not have until render. A fixed `가` renders `세션가` for
    // every value that ends in a consonant. `이(가)` is already-parenthesised and
    // is what this rule asks for, so it must not be flagged.
    const bad = rawOffenders(new RegExp(`\\}\\}(?:을|를|이|가|은|는|와|과|으로|로)(?![${HANGUL}(])`))
    expect(bad, report(bad)).toEqual([])
  })
})

describe('ko terminology (style/ko.md §2.2)', () => {
  // 외래어 표기법 spellings. Unconditional: none of these is correct in any sense,
  // so no English cue is needed to judge them.
  const MISSPELLED: Array<[string, string]> = [
    ['메세지', '메시지'],
    ['쓰레드', '스레드'],
    ['컨텐츠', '콘텐츠'],
    ['데이타', '데이터'],
    ['어플리케이션', '애플리케이션'],
    ['스케쥴', '스케줄'],
    ['캐쉬', '캐시'],
    ['브라우져', '브라우저'],
    ['억세스', '액세스'],
    ['워크플로우', '워크플로'],
    ['유저', '사용자'],
  ]

  for (const [banned, canonical] of MISSPELLED) {
    it(`writes ${canonical}, never ${banned}`, () => {
      const bad = prose
        .filter(([, value]) => value.includes(banned))
        .map(([key, value]) => describeKey(key, value))
      expect(bad, report(bad)).toEqual([])
    })
  }

  // One concept, one word. Each entry is [english cue, banned rendering,
  // canonical rendering]; the cue keeps the check context-sensitive, so a banned
  // string is only a violation where the English proves the sense — `열기` in
  // "Open a file" is untouched.
  const BANNED: Array<[string, string, string]> = [
    ['write access', '라이트 액세스', '쓰기 권한'],
    ['present', '선물', '참석'],
  ]

  for (const [cue, banned, canonical] of BANNED) {
    it(`renders '${cue}' as ${canonical}, never ${banned}`, () => {
      const bad = Object.keys(ko).filter(
        k => (en[k] ?? '').includes(cue) && ko[k].includes(banned),
      )
      expect(bad, report(bad)).toEqual([])
    })
  }
})

describe('ko register (style/ko.md §4)', () => {
  it('never drops into 한다체 in UI copy', () => {
    // 명사형 — a bare noun phrase, `활동 없음` — is the standard form for a UI
    // description and is NOT a violation, so this matches verb endings only.
    const bad = offenders(/(?:한다|된다|이다|하다|온다|간다|본다|았다|었다|겠다|있다|없다|같다)\.?$/)
    expect(bad, report(bad)).toEqual([])
  })

  it('never stacks two passives', () => {
    // `되어집니다` is `되다` + `지다`. The single passive is already in `됩니다`.
    const bad = offenders(/(?:되어지|되어졌|보여지|쓰여지|불려지|잊혀지|나뉘어지)/)
    expect(bad, report(bad)).toEqual([])
  })

  it('spells the honorific imperative and the polite ending correctly', () => {
    // `읍니다` cannot be banned outright: after a stem ending in the vowel ㅡ it is
    // the CORRECT ending (모으다 → 모읍니다). The error is the pre-1988 form on a
    // consonant stem, where the ending is `습니다` — so match those stems, not the
    // suffix alone.
    const bad = offenders(/십시요|(?:있|없|같|좋|많|싶|옳|맞|알|넓|깊)읍니다/)
    expect(bad, report(bad)).toEqual([])
  })

  it('asks a destructive confirmation in 하십시오체', () => {
    // `삭제할까요?` on one dialog and `삭제하시겠습니까?` on another is the same
    // operation in two registers, met by one user in one session — and the softer
    // form reads as a suggestion where the copy has to read as a warning.
    //
    // Judged from the ENGLISH, so a non-destructive offer keeps `~할까요?`, which is
    // the correct form for it: `무엇을 조사할까요?` is not a confirmation.
    const DESTRUCTIVE = /\b(delete|remove|discard|destroy|revert|reset|clear|trash|wipe|purge|recall|overwrite|erase|forget|uninstall|revoke|interrupt(?:ed)?|close)\b/i
    const bad = prose
      .filter(([key, value]) => {
        const source = en[key] ?? ''
        return source.includes('?') && DESTRUCTIVE.test(source) && /까요\?/.test(value)
      })
      .map(([key, value]) => describeKey(key, value))
    expect(bad, report(bad)).toEqual([])
  })

  it('does not end a button or menu label with a full stop', () => {
    // A label is 명사형 with no full stop. Only keys whose ENGLISH is a bare label
    // (no terminal punctuation, at most three words) are judged, so a genuine
    // sentence is never caught.
    const bad = prose
      .filter(([key, value]) => {
        const source = en[key]
        if (source === undefined || /[.!?:…]$/.test(source)) return false
        return source.trim().split(/\s+/).length <= 3 && value.endsWith('.')
      })
      .map(([key, value]) => describeKey(key, value))
    expect(bad, report(bad)).toEqual([])
  })
})

describe('ko plural forms (style/ko.md §5)', () => {
  it('never supplies a plural category Korean does not have', () => {
    // Korean has exactly one CLDR plural category: `other`. A `_one` key is
    // therefore unreachable — and worse, it makes the catalog look like it
    // handles counting when it silently cannot.
    // A key may end in `_one` simply because its English sentence ends with the
    // WORD "one"; only a key with an `_other` sibling is a real plural family.
    const keys = new Set(Object.keys(ko))
    const bad = [...keys].filter(k => {
      const m = k.match(/^(.*)_(one|two|few|many)$/)
      return m !== null && keys.has(`${m[1]}_other`)
    })
    expect(bad, report(bad)).toEqual([])
  })
})
