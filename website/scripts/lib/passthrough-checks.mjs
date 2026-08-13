/**
 * Is a catalog value written in its locale's language, or is it English standing in
 * for a translation?
 *
 * ## Why this is not one of the `CHECKS` in `qa-checks.mjs`
 *
 * Two reasons, and both would silently disable it.
 *
 * `changedValueFindings` excuses a translation when the ENGLISH source trips the same
 * check — an unbalanced English source cannot have a balanced translation. For a
 * language check that exemption is total: the English source is English for every key,
 * so every finding would be excused and the check would measure nothing while passing.
 *
 * And `qa.test.ts` gates every member of that array against a per-check ceiling, which
 * makes the whole-catalog count a failure. The debt here is ~2,700 values across eleven
 * catalogs, none of it the fault of the branch that happens to touch the file next.
 * The whole-catalog number belongs in the report-only tier; only the diff can fail.
 *
 * ## Two mechanisms, because one alphabet cannot answer for eleven locales
 *
 * `untranslated-script` covers the six locales written in a non-Latin script: a value
 * with no character of the target script in it is not in the target language. This is
 * decidable from the codepoints alone.
 *
 * `untranslated-english` covers the five Latin-script locales, where that question
 * cannot be asked of the alphabet at all — `de` and English share it. It fires when the
 * value is typographically identical to the English source, or when English function
 * words outnumber the target's by 3:1. Both need a length floor: below six words the
 * signal is noise, and an identical short value is usually correct (`Status`, `Admin`,
 * `Frontend` are the same word in five languages).
 *
 * ## What is stripped before judging
 *
 * Everything locale-invariant: interpolation placeholders, URLs, paths, filenames, CLI
 * flags, code spans, markup, digits — plus every do-not-translate term from
 * `glossary.json`, which is the exclusion list this check needs and the repo already
 * maintains. A value that is nothing but those is not judged at all, which is what
 * keeps `us-east-1`, `localhost:5173` and `{{count}} cron` out of the findings without
 * a hand-written allowlist.
 */

/** Codepoint ranges per script, enough to classify a letter's writing system. */
const SCRIPT_RANGES = {
  Bengali: [[0x0980, 0x09ff]],
  Devanagari: [[0x0900, 0x097f], [0xa8e0, 0xa8ff], [0x11b00, 0x11b5f]],
  Hiragana: [[0x3041, 0x309f], [0x1b001, 0x1b11f]],
  Katakana: [[0x30a0, 0x30ff], [0x31f0, 0x31ff], [0xff66, 0xff9d]],
  Han: [
    [0x2e80, 0x2eff], [0x3005, 0x3005], [0x3007, 0x3007], [0x3400, 0x4dbf],
    [0x4e00, 0x9fff], [0xf900, 0xfaff], [0x20000, 0x2a6df], [0x2a700, 0x2ebef],
    [0x2f800, 0x2fa1f],
  ],
  Hangul: [[0x1100, 0x11ff], [0x3130, 0x318f], [0xa960, 0xa97f], [0xac00, 0xd7ff], [0xffa0, 0xffdc]],
  Cyrillic: [[0x0400, 0x052f], [0x1c80, 0x1c8f], [0x2de0, 0x2dff], [0xa640, 0xa69f]],
}

/**
 * The script a translation of each of these locales must actually be written in.
 * A locale absent from this map is never judged by `untranslated-script` — that
 * includes `en`, the generated pseudolocale, and the five Latin-script locales.
 */
export const TARGET_SCRIPTS = {
  bn: ['Bengali'],
  hi: ['Devanagari'],
  ja: ['Hiragana', 'Katakana', 'Han'],
  ko: ['Hangul'],
  ru: ['Cyrillic'],
  'zh-CN': ['Han'],
}

/**
 * Locale-invariant spans, removed before any language judgement. Order matters:
 * fenced and inline code go first so their contents are never mined for words, and
 * digits go last so a version or a size cannot be mistaken for prose.
 */
const NOISE = [
  /```[\s\S]*?```/g,
  /`[^`]*`/g,
  /(?:https?|ftp|file|mailto):\/\/\S+|\bwww\.[\w.-]+\S*/g,
  /[\w.+-]+@[\w-]+(?:\.[\w-]+)+/g,
  /[A-Za-z]:\\[^\s,;]*/g,
  /~\/[\w./-]*/g,
  /(?:\/[\w.-]+){2,}\/?/g,
  /\b[\w.-]+\/[\w./-]*\.\w+\b/g,
  /\{\{[^{}]*\}\}/g,
  /\$\{[^{}]*\}/g,
  /\{[^{}]*\}/g,
  /%\([^)]*\)[sdifr]|%[sdifr]\b|%\d*\$?[sd]/g,
  /<\/?[A-Za-z][\w.:-]*(?:\s+[^<>]*?)?\/?>|<[a-z_][\w.-]*>/g,
  /(?<![\w-])--?[A-Za-z][\w-]*/g,
  /\b[\w-]+\.(?:json|py|ts|tsx|js|jsx|md|yml|yaml|toml|sh|html|css|png|jpg|svg|log|txt|lock|cfg|ini|env)\b/g,
]

/**
 * Removed separately from `NOISE`, and with NOTHING in their place.
 *
 * Substituting a space would split a sample identifier such as `E0123ABC456` into two
 * tokens and so defeat the single-token exemption, reporting one opaque literal as if
 * it were a sentence.
 */
const DIGITS = /\d+/g

const LETTER = /\p{L}/u
const NON_WORD = /[^\p{L}\p{N}\s']/gu

/** Escape a DNT term for use in a word-boundary regex. */
const escapeRe = s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

/**
 * A term is removed only where it stands as a word, so `Git` does not eat the `Git`
 * inside a longer identifier and leave a fragment behind to be scored.
 */
const dntPattern = terms =>
  (terms.length
    ? new RegExp(`(?<![\\p{L}\\p{N}])(?:${terms.map(escapeRe).join('|')})(?![\\p{L}\\p{N}])`, 'giu')
    : null)

/** Strip every locale-invariant span, then the DNT terms, then collapse whitespace. */
export function strippedProse(value, dntRe = null) {
  let out = value
  for (const rx of NOISE) out = out.replace(rx, ' ')
  if (dntRe) out = out.replace(dntRe, ' ')
  out = out.replace(DIGITS, '')
  return out.replace(/\s+/g, ' ').trim()
}

/** Lowercased word tokens of the stripped prose. */
export function words(stripped) {
  return stripped.toLowerCase().replace(NON_WORD, ' ').split(/\s+/).filter(Boolean)
}

/**
 * Whitespace-separated tokens that contain a letter, which is NOT the same count as
 * `words`.
 *
 * The single-token guard exists to exempt labels and identifiers, and those are held
 * together by punctuation: splitting on it would read `us-east-1` as two tokens and
 * judge a region name as a sentence. Punctuation left standing alone is not a token
 * either — counting it made `cleared ·`, `& Issue` and `Worktrees ({{count}})` look
 * like two-word phrases and reported one word each as untranslated prose.
 */
export function tokenCount(stripped) {
  return stripped.split(/\s+/).filter(t => LETTER.test(t)).length
}

const scriptOf = cp => {
  for (const [name, ranges] of Object.entries(SCRIPT_RANGES)) {
    for (const [lo, hi] of ranges) if (cp >= lo && cp <= hi) return name
  }
  return 'Other'
}

/**
 * Fraction of the value's letters that belong to the locale's script.
 *
 * Combining marks are excluded from both sides: a Bengali or Devanagari vowel sign
 * never appears without a base letter of its own script, so counting it would change
 * nothing but the denominator.
 */
export function scriptRatio(stripped, scripts) {
  const want = new Set(scripts)
  let letters = 0
  let onTarget = 0
  for (const ch of stripped) {
    if (!LETTER.test(ch)) continue
    letters += 1
    if (want.has(scriptOf(ch.codePointAt(0)))) onTarget += 1
  }
  return { letters, onTarget, ratio: letters === 0 ? null : onTarget / letters }
}

// ---- Latin-script locales: English function words vs the target's ------------

const EN_FUNCTION = `the a an and or but not no nor of to in on at by for with from into over
under after before between through during without within about above below again further as is
are was were be been being has have had having do does did doing can could will would shall
should may might must this that these those there here what which who whom whose how why when
where while until because since though although unless whether if so than too very just now
then once all any each both few more most other some such only own same up down out off also
still even ever never always already instead rather however therefore thus hence else otherwise
you your yours we our ours they their them theirs it its he she his her him us me my mine i`

/**
 * Function words of each Latin-script locale, including the ones that collide with
 * English. The collisions are what the scorer must NOT count, and they can only be
 * subtracted if both lists name them: a target list missing Romance `a` or Italian
 * `me` scores `Assegnate a me` as English on the strength of its own vocabulary.
 */
const TARGET_FUNCTION = {
  de: `der die das den dem des ein eine einen einem einer eines und oder aber nicht kein keine
keinen mit von zu zur zum fuer für auf aus bei nach über unter durch ohne gegen um an im in ist
sind war waren wird werden wurde wurden hat haben hatte kann können muss müssen soll sollen darf
dürfen wenn dass weil damit wie was wer wo welche welcher welches diese dieser dieses sich sie
er es ich du wir ihr ihnen mein dein sein ihre unser alle auch noch nur schon sehr mehr dann
hier dort jetzt bereits wieder immer nie sowie beim vom dazu dabei dafür danach davor als bis
etwa jedoch sondern zwar dessen deren wobei sobald falls so man da`,
  es: `el la los las un una unos unas de del al y o pero no que con para por en sin sobre entre
desde hasta hacia es son era eran ser está están estar hay tiene tienen puede pueden debe deben
se su sus tu tus mi mis este esta estos estas ese esa eso cuando como donde si más menos muy ya
también aún todavía siempre nunca cada todo toda todos todas otro otra algún alguna ningún lo le
les nos te me ha han había será sea aquí allí ahora luego entonces porque aunque mientras antes
después durante cual cuales quien quienes a son van he mas`,
  fr: `le la les un une des du de au aux et ou mais ne pas que qui quoi dont où avec pour par en
dans sur sous sans entre depuis vers chez est sont était étaient être a ont avait avoir peut
peuvent doit doivent se son sa ses votre vos mon ma mes notre nos ce cette ces cet quand comme
comment si plus moins très déjà aussi encore toujours jamais chaque tout toute tous toutes autre
autres ici là maintenant ensuite alors parce donc ainsi avant après pendant lors leur leurs nous
vous elle il ils elles je tu on cela ceci ceux celle car lorsque afin sur or ces no`,
  it: `il lo la i gli le un uno una del della dei delle dal dalla al alla ai alle nel nella e ed
o ma non che chi con per tra fra da di in su senza sopra sotto è sono era erano essere ha hanno
aveva avere può possono deve devono si suo sua suoi sue tuo tua mio mia questo questa questi
queste quello quella quando dove se più meno molto già anche ancora sempre mai ogni tutto tutta
tutti tutte altro altra qui qua ora poi allora perché mentre prima dopo durante loro noi voi lei
lui io tu ci vi ne dell nell all cui quindi però oppure a me come do no`,
  pt: `o a os as um uma uns umas do da dos das no na nos nas ao aos à às e ou mas não nao que
quem com para por em sem sobre entre desde até é são era eram ser está estão estar há tem têm
pode podem deve devem se seu sua seus suas teu meu minha este esta estes estas esse essa isso
quando como onde mais menos muito já também ainda sempre nunca cada todo toda todos todas outro
outra aqui ali agora depois então porque enquanto antes durante deles nós você ele ela eles elas
eu tu lhe lhes qual quais pelo pela pelos pelas num numa me ha`,
}

const wordSet = spec => new Set(spec.split(/\s+/).filter(Boolean))

const EN_ALL = wordSet(EN_FUNCTION)

/**
 * Per locale: the English function words that are NOT also the target's, and the
 * target's that are not also English. Everything in the intersection is invisible to
 * the scorer, in both directions.
 */
export const FUNCTION_WORDS = Object.fromEntries(
  Object.entries(TARGET_FUNCTION).map(([lang, spec]) => {
    const target = wordSet(spec)
    const collisions = new Set([...EN_ALL].filter(w => target.has(w)))
    return [lang, {
      collisions: [...collisions].sort(),
      enOnly: new Set([...EN_ALL].filter(w => !collisions.has(w))),
      targetOnly: new Set([...target].filter(w => !collisions.has(w))),
    }]
  }),
)

/** Typographic variants that make an otherwise identical value look different. */
export const normalizeTypography = s =>
  s
    .replace(/[\u2018\u2019\u02bc\u2032]/g, "'")
    .replace(/[\u201c\u201d\u2033]/g, '"')
    .replace(/[\u2010-\u2015\u2212]/g, '-')
    .replace(/[\u00a0\u202f\u2009\u200a]/g, ' ')
    .replace(/\u2026/g, '...')
    .replace(/\s+/g, ' ')
    .trim()

/** Minimum letters in a non-Latin value before its script is worth judging. */
export const MIN_LETTERS = 4
/** A single token is a label or an identifier, not prose. */
export const MIN_TOKENS = 2
/** Minimum words before an English-vs-target judgement means anything. */
export const MIN_WORDS = 6
/** English function words needed, and by what margin over the target's. */
export const MIN_EN_HITS = 2
export const EN_MARGIN = 3

/**
 * The two checks, shaped like `CHECKS` in `qa-checks.mjs` so the reporting is the
 * same, plus a third argument: the English source for the key, which the
 * identical-to-English half cannot do without.
 *
 * `dnt` is passed in rather than read here so this module stays free of file I/O and
 * the caller keeps one source of truth for the term list.
 */
export function passthroughChecks(dnt = []) {
  const dntRe = dntPattern(dnt)
  return [
    {
      id: 'untranslated-script',
      describe: 'a value must contain at least one character of its locale\'s script',
      violates: (value, lang) => {
        const scripts = TARGET_SCRIPTS[lang]
        if (!scripts) return false
        const stripped = strippedProse(value, dntRe)
        const { letters, onTarget } = scriptRatio(stripped, scripts)
        if (letters < MIN_LETTERS) return false
        if (tokenCount(stripped) < MIN_TOKENS) return false
        return onTarget === 0
      },
    },
    {
      id: 'untranslated-english',
      describe: 'a Latin-script value must not be the English source or read as English',
      violates: (value, lang, source) => {
        const sets = FUNCTION_WORDS[lang]
        if (!sets) return false
        const stripped = strippedProse(value, dntRe)
        const tokens = words(stripped)
        if (tokens.length < MIN_WORDS) return false
        if (
          typeof source === 'string'
          && normalizeTypography(stripped)
            === normalizeTypography(strippedProse(source, dntRe))
        ) return true
        let en = 0
        let target = 0
        for (const t of tokens) {
          if (sets.enOnly.has(t)) en += 1
          else if (sets.targetOnly.has(t)) target += 1
        }
        return en >= MIN_EN_HITS && en >= EN_MARGIN * target
      },
    },
  ]
}

/**
 * Findings among the values a branch ADDED or CHANGED, at zero tolerance.
 *
 * There is no inherited-defect exemption and there must not be one: a new key whose
 * translation was filled in with the English source is precisely the defect, and
 * excusing it because English "also fails" would excuse every case.
 */
export function passthroughFindings({ lang, base, head, enHead = {}, checks }) {
  const findings = []
  for (const [key, value] of Object.entries(head)) {
    if (typeof value !== 'string' || !value) continue
    if (base[key] === value) continue
    for (const check of checks) {
      if (check.violates(value, lang, enHead[key])) findings.push({ lang, key, value, check })
    }
  }
  return findings
}

/** Every violation in a whole catalog — the report-only inventory, never a gate. */
export function passthroughViolations({ lang, catalog, enFlat = {}, checks }) {
  const findings = []
  for (const [key, value] of Object.entries(catalog)) {
    if (typeof value !== 'string' || !value) continue
    for (const check of checks) {
      if (check.violates(value, lang, enFlat[key])) findings.push({ lang, key, value, check })
    }
  }
  return findings
}
