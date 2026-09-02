/**
 * The two language checks behind `changed-passthrough` and `untranslated-passthrough`.
 *
 * All the judgement in this feature lives in two predicates and four numbers, and the
 * numbers are not arbitrary — each was chosen against the shipped catalogs, where a
 * looser one produced a measured false positive. A false positive here is expensive in
 * a way a missed defect is not: the diff-scoped check has no ceiling to raise, so a
 * wrong finding blocks a PR with no way around it but to argue. Every case below that
 * asserts `false` is therefore a guard rail, not a nicety, and the ones naming a
 * specific string are the strings that actually broke an earlier rule.
 */
import { describe, it, expect } from 'vitest'

import {
  FUNCTION_WORDS,
  MIN_WORDS,
  TARGET_SCRIPTS,
  passthroughChecks,
  strippedProse,
  tokenCount,
} from '../../scripts/lib/passthrough-checks.mjs'

/** The real do-not-translate list is small and stable; these are enough of it. */
const DNT = [
  'AWS', 'GitHub', 'Kiro', 'Python', 'Slack', 'JSON', 'MCP', 'npm', 'Git',
  'KiroCrew', // brand-ok: the product name as `dnt` itself spells it
]

const [script, english] = passthroughChecks(DNT)
const flagsScript = (v: string, lang: string) => script.violates(v, lang)
const flagsEnglish = (v: string, lang: string, source?: string) =>
  english.violates(v, lang, source)

describe('untranslated-script — a value must carry its locale’s script', () => {
  it('flags plain English in every non-Latin locale', () => {
    for (const lang of Object.keys(TARGET_SCRIPTS)) {
      expect(flagsScript('Host memory is tight', lang), lang).toBe(true)
    }
  })

  it('accepts a genuine translation in each of them', () => {
    const translated: Record<string, string> = {
      ja: 'ホストのメモリが不足しています',
      'zh-CN': '主机内存不足，请避免并行任务',
      ko: '호스트 메모리가 부족합니다',
      ru: 'Недостаточно памяти на хосте',
      hi: 'होस्ट मेमोरी कम है',
      bn: 'হোস্ট মেমরি কম আছে',
    }
    for (const [lang, value] of Object.entries(translated)) {
      expect(flagsScript(value, lang), lang).toBe(false)
    }
  })

  it('accepts a translation that keeps an English product name in it', () => {
    // The common and correct shape: prose in the target script, brand left alone.
    expect(flagsScript('GitHub のトークンを設定してください', 'ja')).toBe(false)
    expect(flagsScript('在 Slack 中打开此会话', 'zh-CN')).toBe(false)
  })

  it('never judges English, the pseudolocale, or a Latin-script locale', () => {
    for (const lang of ['en', 'en-XA', 'de', 'fr', 'es', 'it', 'pt']) {
      expect(flagsScript('Host memory is tight', lang), lang).toBe(false)
    }
  })

  it('leaves short labels and abbreviations alone', () => {
    // Below four letters there is no language in a string, only a label. These are all
    // real standalone values in the shipped catalogs.
    for (const v of ['OK', 'ID', 'CPU', 'MEM', 'DSK', 'v', 'Aa']) {
      expect(flagsScript(v, 'ja'), v).toBe(false)
    }
  })

  it('leaves a single token alone however long it is', () => {
    // One token is an identifier, a brand or a status word — not a sentence. Hyphens and
    // colons hold a token together, which is why the count is whitespace-based.
    for (const v of ['us-east-1', 'localhost:5173', 'BucketOwnerEnforced', 'STAYGREEN',
      'profiles/*.json', 'github_pat_…', 'mcpServers']) {
      expect(flagsScript(v, 'zh-CN'), v).toBe(false)
    }
  })

  it('ignores a value that is only placeholders, markup and noise', () => {
    for (const v of ['{{count}} cron', '(v{{from}} → v{{to}})', '`npm ci`', '0 B', '120s',
      'https://example.com/docs', '~/.kiro/crew', '--no-verify']) {
      expect(flagsScript(v, 'ru'), v).toBe(false)
    }
  })

  it('ignores a value made only of do-not-translate terms', () => {
    // This is what keeps the exclusion list out of a hand-maintained allowlist: register
    // the product name once in glossary.json and every locale stops reporting it.
    expect(flagsScript('GitHub MCP', 'ja')).toBe(false)
    expect(flagsScript('AWS JSON', 'ko')).toBe(false)
  })
})

describe('untranslated-english — a Latin-script value must not read as English', () => {
  const LONG_EN = 'Host memory is critically low and heavy parallel work should be avoided'

  it('flags English prose in every Latin-script locale', () => {
    for (const lang of Object.keys(FUNCTION_WORDS)) {
      expect(flagsEnglish(LONG_EN, lang), lang).toBe(true)
    }
  })

  it('flags a value that is the English source verbatim', () => {
    const source = 'Every configured source answered the last poll successfully.'
    expect(flagsEnglish(source, 'de', source)).toBe(true)
  })

  it('flags a value differing from the source only in typography', () => {
    // Measured on the shipped catalogs: five locales store the same English string as
    // the source with a curly apostrophe, which byte-identity alone does not catch.
    const source = "The parent instance's configuration is required for this to work."
    const value = 'The parent instance\u2019s configuration is required for this to work.'
    expect(flagsEnglish(value, 'fr', source)).toBe(true)
  })

  it('accepts genuine translations', () => {
    const translated: Record<string, string> = {
      de: 'Der Hostspeicher ist knapp, daher kann schwere Arbeit langsamer werden.',
      es: 'La memoria del host es escasa, por lo que el trabajo pesado puede ser lento.',
      fr: 'La mémoire de l’hôte est faible, donc le travail lourd peut être plus lent.',
      it: 'La memoria dell’host è insufficiente, quindi il lavoro pesante può rallentare.',
      pt: 'A memória do host está baixa, portanto o trabalho pesado pode ficar mais lento.',
    }
    for (const [lang, value] of Object.entries(translated)) {
      expect(flagsEnglish(value, lang), lang).toBe(false)
    }
  })

  it('does not fire on the cognate collisions that broke a hand-written list', () => {
    // `a` is a preposition in every Romance language and an article in English; `me` and
    // `do` collide the same way. A scorer that counted them as English evidence flagged
    // these real translated values — which is why the collision set is DERIVED from the
    // intersection of the two lists rather than typed out.
    expect(flagsEnglish('Assegnate a me e ancora da completare oggi', 'it')).toBe(false)
    expect(flagsEnglish('Asignadas a mi cuenta y todavia sin completar hoy', 'es')).toBe(false)
    expect(flagsEnglish('Adicionar a base de conhecimento do seu projeto agora', 'pt')).toBe(false)
  })

  it('derives the collision set in both directions', () => {
    for (const [lang, sets] of Object.entries(FUNCTION_WORDS)) {
      for (const w of sets.collisions) {
        expect(sets.enOnly.has(w), `${lang} ${w}`).toBe(false)
        expect(sets.targetOnly.has(w), `${lang} ${w}`).toBe(false)
      }
    }
    // The two that a hand-written list missed, and cost real false positives.
    expect(FUNCTION_WORDS.it.collisions).toContain('a')
    expect(FUNCTION_WORDS.it.collisions).toContain('me')
    expect(FUNCTION_WORDS.pt.collisions).toContain('a')
  })

  it('says nothing below the word floor, in either direction', () => {
    // Under six words the signal is noise, and an identical short value is usually
    // correct: `Status`, `Admin` and `Frontend` are the same word in five languages.
    for (const v of ['Status', 'Admin', 'Frontend', 'Pull requests', 'Max cycles',
      'Kiro Crew Dashboard']) {
      expect(flagsEnglish(v, 'de', v), v).toBe(false)
    }
    const five = 'Host memory is very low'
    expect(five.split(' ').length).toBeLessThan(MIN_WORDS)
    expect(flagsEnglish(five, 'de', five)).toBe(false)
  })

  it('never judges English, the pseudolocale, or a non-Latin locale', () => {
    for (const lang of ['en', 'en-XA', 'ja', 'zh-CN', 'ru', 'ko', 'hi', 'bn']) {
      expect(flagsEnglish(LONG_EN, lang), lang).toBe(false)
    }
  })
})

describe('stripping', () => {
  it('removes every locale-invariant span before anything is judged', () => {
    expect(strippedProse('Open {{name}} at https://x.dev/a/b using `npm ci` v2')).toBe('Open at using v')
  })

  it('removes a do-not-translate term only where it stands as a word', () => {
    const dntRe = passthroughChecks(['Git'])
    // Quick check: the factory returns checks, and the term itself disappears...
    expect(dntRe).toHaveLength(2)
    expect(strippedProse('Git is required', /(?<![\p{L}\p{N}])(?:Git)(?![\p{L}\p{N}])/giu))
      .toBe('is required')
    // ...but a longer identifier containing it is left whole, so no fragment is scored.
    expect(strippedProse('GitLab is required', /(?<![\p{L}\p{N}])(?:Git)(?![\p{L}\p{N}])/giu))
      .toBe('GitLab is required')
  })

  it('counts tokens by whitespace, not by punctuation', () => {
    expect(tokenCount('us-east-1')).toBe(1)
    expect(tokenCount('localhost:5173')).toBe(1)
    expect(tokenCount('Host memory is tight')).toBe(4)
  })

  it('does not count punctuation left standing alone as a token', () => {
    // Real values from the shipped catalogs. Each is ONE word plus punctuation, which is
    // exactly what the single-token exemption is for — counting the bullet or the
    // ampersand reported them as two-word English phrases.
    for (const v of ['cleared ·', '· main', '& Issue', 'Worktrees ({{count}})',
      'Transcribe (AWS)', 'Apache 2.0', '{{label}} — Kiro Crew']) {
      expect(flagsScript(v, 'ja'), v).toBe(false)
    }
  })

  it('removes digits without splitting the token around them', () => {
    // Substituting a space would turn a sample identifier into `E ABC` — two tokens,
    // four letters — and report an opaque literal as a sentence.
    expect(strippedProse('E0123ABC456')).toBe('EABC')
    expect(tokenCount(strippedProse('E0123ABC456'))).toBe(1)
    expect(flagsScript('E0123ABC456', 'ko')).toBe(false)
    expect(flagsScript('U0123ABC456', 'ru')).toBe(false)
  })
})
