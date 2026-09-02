/**
 * settingsSearchCore — the ONE scorer for settings search.
 *
 * Both surfaces that search SETTINGS_REGISTRY call this module:
 *  - the Search Everywhere settings provider (`providers/settingsProvider.ts`)
 *  - the in-page Settings search box (`pages/settings/SettingsSearch.tsx`)
 *
 * It exists because the two grew separate copies of the corpus/rank logic and
 * immediately diverged (the localized-label corpus existed only in-page, so
 * the palette stayed English-only). A keyword or scoring fix must land once,
 * for both, or the same query ranks differently depending on where it is typed.
 *
 * Matching is PER PART and, for non-label parts, SUBSTRING-gated: a
 * subsequence that only exists by scattering letters across fields ("yolo"
 * spanning label+description+keywords), or through one long description ("so
 * You can fOLlow alOng"), is noise that buries real hits and trains users to
 * distrust the box. Labels keep full fuzzy matching — typo tolerance belongs
 * on the field the user is actually naming; a description or keyword counts
 * only when it literally contains the query. The deliberate trade: a query
 * spanning two fields ("voice speed" = tab + label) no longer matches — the
 * tab-prefix syntax (`voice: speed`) is the sanctioned cross-field query, and
 * the old joined corpus only ever matched one ordering of such queries anyway
 * (subsequences are order-dependent).
 *
 * Ranking tiers, best first:
 *  1. Label hit — the row IS the term (English label or localized label).
 *  2. Whole-word keyword hit — the query aligns with a word of a curated
 *     synonym ("yolo", "shutdown" in "until shutdown"). Ranks WITH labels
 *     (minus a 1-point tie edge, see keywordRankScore): synonyms are the
 *     advertised path to settings whose labels don't contain the term, so
 *     discounting them let scattered-subsequence label noise ("Your Role"
 *     for "yolo") outrank the intended target.
 *  3. Any other part hit, discounted ×0.6 — the row merely mentions the term
 *     in its description/keywords/tab.
 */
import { fuzzyMatch } from '../../utils/fuzzyMatch'
import { i18nT } from '../../i18n/t'
import { SETTINGS_KEYWORDS } from './settingsKeywords'
import { settingsTabLabel } from './settingsTabLabel'
import type { SettingEntry } from './settingsTypes'

export interface SettingEntryScore {
  /** Comparable score (same scale as fuzzyMatch). Higher is better. */
  score: number
  /** Match indices into `localizedLabel`, for highlight marks. Empty when the
   *  hit came from keywords, description, or the tab name. (In the English
   *  locale `localizedLabel` equals `entry.label`, so these highlight the
   *  English label there.) */
  indices: number[]
  /** Label as rendered in the active locale, fan-out suffix re-appended. */
  localizedLabel: string
}

/**
 * The entry's label in the active locale. Resolving `labelKey` drops the
 * fan-out suffix baked into `entry.label` ("Bot Token (Discord)" → "Bot
 * Token"), which would render per-channel entries as indistinguishable rows —
 * re-append it.
 */
export function localizedSettingLabel(entry: SettingEntry): string {
  const base = entry.labelKey ? i18nT(entry.labelKey) : entry.label
  return entry.labelKey && entry.labelSuffix ? `${base} (${entry.labelSuffix})` : base
}

/** Separators fuzzyMatch treats as word boundaries, normalized to spaces so the
 *  whole-word predicate below sees "auto-approve" and "auto approve" alike. */
const SEPARATORS_RE = /[-_/.:\\|]+/g

/**
 * A keyword hit that deserves label rank: the query starts the keyword or
 * starts one of its later words (hyphen/underscore/etc. count as word breaks,
 * matching fuzzyMatch's own separator set — so "speech" promotes on
 * "text-to-speech"). Scattered subsequences through a keyword do NOT qualify —
 * they stay in the discounted corpus tier, or synonym lists would become a
 * noise amplifier instead of a precision tool.
 *
 * Scored 1 point under the raw fuzzy score: a curated synonym ranks WITH
 * labels but a row whose LABEL is the term wins an exact-score tie (query
 * "theme" → the Theme setting above Mode-with-keyword-'theme'; the name-based
 * tiebreak would otherwise pick alphabetically).
 */
function keywordRankScore(query: string, kws: readonly string[] | undefined): number {
  if (!kws) return 0
  const q = query.toLowerCase().replace(SEPARATORS_RE, ' ')
  let best = 0
  for (const kw of kws) {
    const k = kw.toLowerCase().replace(SEPARATORS_RE, ' ')
    if (!(k.startsWith(q) || k.includes(' ' + q))) continue
    const m = fuzzyMatch(query, kw)
    if (m && m.score > best) best = m.score
  }
  return best > 0 ? best - 1 : 0
}

/**
 * Score one registry entry against a query, or `null` for no match at all.
 *
 * `includeTab` (default true) adds the tab key and its localized display name
 * as matchable parts; tab-scoped searches pass `false` so the (constant) tab
 * name cannot produce or distort hits within the tab.
 */
export function scoreSettingEntry(
  query: string,
  entry: SettingEntry,
  opts: { includeTab?: boolean } = {},
): SettingEntryScore | null {
  const localizedLabel = localizedSettingLabel(entry)
  const kws = SETTINGS_KEYWORDS[entry.id]

  const localizedLabelMatch = fuzzyMatch(query, localizedLabel)
  const labelScore = Math.max(
    localizedLabel !== entry.label ? fuzzyMatch(query, entry.label)?.score ?? 0 : 0,
    localizedLabelMatch?.score ?? 0,
  )
  const strong = Math.max(labelScore, keywordRankScore(query, kws))

  let score = strong
  if (strong <= 0) {
    // Corpus tier: best single-part hit, discounted — and only for parts that
    // CONTAIN the query. fuzzyMatch still provides the score (so a hit at a
    // word boundary outranks one mid-word), but the containment gate is what
    // keeps a 40-word description from matching every 4-letter query as a
    // scattered subsequence. See the module comment.
    const q = query.trim().toLowerCase()
    const parts: string[] = []
    if (entry.description) parts.push(entry.description)
    if (kws) parts.push(...kws)
    if (opts.includeTab !== false) {
      parts.push(entry.tab)
      parts.push(settingsTabLabel(entry.tab))
    }
    let corpus = 0
    for (const part of parts) {
      if (!part.toLowerCase().includes(q)) continue
      const m = fuzzyMatch(query, part)
      if (m && m.score > corpus) corpus = m.score
    }
    if (corpus <= 0) return null
    score = Math.max(1, Math.round(corpus * 0.6))
  }
  return { score, indices: localizedLabelMatch?.indices ?? [], localizedLabel }
}
