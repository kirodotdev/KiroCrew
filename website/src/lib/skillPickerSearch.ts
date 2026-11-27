/**
 * Matching and ranking for the composer's `$skill` picker (#42694).
 *
 * A skill's `key` is its path under the skills root (`ops/oncall/handoff-summary`)
 * and its `name` is the `name:` from SKILL.md (`ops-oncall-handoff-summary`),
 * which may differ from the last folder when a long list is grouped into nested
 * folders. The picker used to match only the last folder, so `$ops`, `$oncall`
 * and `$data-warehouse` found nothing although skill_search finds those skills
 * by either field. Every row is now matched, case-insensitively, against:
 *
 *   1. the `name:` from SKILL.md
 *   2. the last folder (what the inserted `$token` resolves on)
 *   3. every other path segment (`ops`, `oncall`)
 *   4. the full key as one string (`ops/oncall`)
 *
 * and ranked exact and prefix matches on the name and last folder first, then
 * path segments, then substrings, then the word-start fuzzy matches the picker
 * already had (`$tpull` → `ticket-pull`). Rows inside a tier keep catalog
 * order, except the fuzzy tier, which sorts best score first.
 *
 * Rows are no longer deduped by last folder: two groups' same-leaf skills both
 * list, told apart by the path line. The inserted token stays `$leaf` while
 * the leaf is unique in the catalog; for a shared leaf it is the full key
 * (`$ops/oncall/escalation`), which `resolve_dollar_skills` matches exactly
 * where a bare ambiguous `$escalation` resolves to nothing. Both forms are
 * matched against the enumerated catalog only, so nothing here widens what a
 * token can address.
 */
import { makeScoreThenNameComparator } from '../utils/fuzzyMatch'

export interface SkillPickerRow {
  key: string
  name: string
  /** Other keys the listing dropped for this same SKILL.md (see loaderKey). */
  aliases?: string[]
  /** `kiro-workspace` rows only: false until the operator trusts the project. */
  trusted?: boolean
}

// The `package/<q>:<rel>` root qualifier minted by the backend's
// `package_root_qualifier`; `split_package_key` accepts only this exact shape.
const PACKAGE_QUALIFIER = /^[0-9a-f]{16}:(?=.)/

/**
 * Last path segment of a skill key — this is what `$token` matches against.
 * The `$` token grammar stops at `:`, so a qualified copy is invoked by its
 * bare name and the agent's mapping picks the copy.
 */
export function leafOf(key: string): string {
  const i = key.lastIndexOf('/')
  const leaf = i === -1 ? key : key.slice(i + 1)
  return leaf.replace(PACKAGE_QUALIFIER, '')
}

// Whether `leaf[i]` starts a word: the first char, a char after a separator
// (anything not a letter or digit), or a lower→Upper camelCase step.
function startsWord(leaf: string, i: number): boolean {
  if (i === 0) return true
  const prev = leaf[i - 1]
  if (!/[a-z0-9]/i.test(prev)) return true
  return /[a-z]/.test(prev) && /[A-Z]/.test(leaf[i])
}

/**
 * Fuzzy score of a leaf that does NOT contain the query as a substring, or
 * null. The query must split into runs that each start a word of the leaf,
 * so `$tpull` finds `ticket-pull`, `$tc` finds `ticket-close` and `$onhand`
 * finds `oncall-handover`, while a stray word such as `$hello` does not pick
 * up a skill whose letters merely appear in order — Enter selects the top
 * row, so a loose match would swallow the send. Every alignment is tried, not
 * only the leftmost one: `$ps` reaches the `s` of `shopper` in
 * `personal-shopper` past the mid-word `s` of `personal`. Fewer runs score
 * higher, then a shorter leaf.
 */
export function wordFuzzyScore(query: string, leaf: string): number | null {
  const q = query.toLowerCase()
  const l = leaf.toLowerCase()
  if (!q) return null
  // Word starts read the original casing (camelCase) only when lowercasing
  // kept every index in place; a few characters lowercase to two.
  const cased = l.length === leaf.length ? leaf : l
  // Fewest runs that place q[qi..] after a match at `prev` (-1 = none yet),
  // or Infinity. Memoized on (qi, prev): a leaf and a query are both short,
  // so this is at most |q| * |leaf| states.
  const memo = new Map<number, number>()
  const runs = (qi: number, prev: number): number => {
    if (qi === q.length) return 0
    const key = qi * (l.length + 1) + prev + 1
    const hit = memo.get(key)
    if (hit !== undefined) return hit
    let best = Infinity
    // Extend the current run.
    if (prev >= 0 && l[prev + 1] === q[qi]) best = runs(qi + 1, prev + 1)
    // Or start a new run at a later word start.
    for (let i = prev + 1; i < l.length; i++) {
      if (l[i] !== q[qi] || !startsWord(cased, i)) continue
      best = Math.min(best, 1 + runs(qi + 1, i))
    }
    memo.set(key, best)
    return best
  }
  const n = runs(0, -1)
  if (!Number.isFinite(n)) return null
  return 1000 - 10 * n - leaf.length
}

// Match tiers, best first. A lower tier number lists earlier.
const TIER_EXACT = 0      // query equals the name or the last folder
const TIER_PREFIX = 1     // the name or the last folder starts with the query
const TIER_SEGMENT = 2    // another path segment equals or starts with the query
const TIER_SUBSTRING = 3  // the name, a segment or the full key contains the query
const TIER_FUZZY = 4      // word-start fuzzy on the name or the last folder

// The fuzzy alignment costs about |query| * |candidate|^2 in the worst case
// and runs on every keystroke, synchronously, for every row that has no
// substring match. A SKILL.md `name:` has no length bound on the backend, so
// one oversized name would freeze the composer; a key leaf is short by
// construction. Any real name fits well within this; past it only the exact,
// prefix and substring tiers still see the whole name.
const FUZZY_NAME_LIMIT = 128

/** Rank of a row against a lowercased, non-empty query, or null for no match. */
function matchTier(row: SkillPickerRow, q: string): { tier: number; score: number } | null {
  const key = row.key || row.name
  const name = (row.name || key).toLowerCase()
  const segments = key.toLowerCase().split('/')
  const leaf = leafOf(key).toLowerCase()
  if (name === q || leaf === q) return { tier: TIER_EXACT, score: 0 }
  if (name.startsWith(q) || leaf.startsWith(q)) return { tier: TIER_PREFIX, score: 0 }
  if (segments.some(seg => seg === q || seg.startsWith(q))) return { tier: TIER_SEGMENT, score: 0 }
  if (name.includes(q) || key.toLowerCase().includes(q)) return { tier: TIER_SUBSTRING, score: 0 }
  const score = Math.max(
    wordFuzzyScore(q, leafOf(key)) ?? -Infinity,
    wordFuzzyScore(q, (row.name || key).slice(0, FUZZY_NAME_LIMIT)) ?? -Infinity,
  )
  if (!Number.isFinite(score)) return null
  return { tier: TIER_FUZZY, score }
}

// The backend's `$token` grammar (skills._DOLLAR_SKILL_PATTERN): lowercase
// letters, digits, `/`, `_` and `-`, so a qualified key is only insertable when
// it fits; a key with an uppercase folder or a package `:` qualifier is not.
const DOLLAR_TOKEN = /^[a-z0-9][a-z0-9/_-]*$/

// `/api/skills` spells some rows under a source prefix the loader never uses:
// a project row is `kiro-workspace/<rel>` so the dashboard can tell it from a
// same-named global skill, a row from the native `~/.kiro/skills` folder is
// `kiro-user/<rel>`, and an edition-contributed row is `package/<rel>` (or
// `package/<16hex>:<rel>` when two edition roots ship the same path)
// (_shared_skill_listing.py, _shared_skill_catalog.py, _shared_skill_roots.py).
// `resolve_dollar_skills` matches the loader's own keys only, so these
// prefixes are presentation and never part of a resolvable token. The
// dashboard reserves the spellings (READONLY_SKILL_KEY_PREFIXES refuses to
// create a managed skill under them), so no managed key starts with one.
const DISPLAY_PREFIXES = ['kiro-workspace/', 'kiro-user/', 'package/']

const WORKSPACE_PREFIX = 'kiro-workspace/'

/**
 * What the token rules need about one catalog, computed in one pass so that
 * ranking, rendering and selection never rescan `items` per row: ten thousand
 * nested skills must cost a few milliseconds per keystroke, not a hundred
 * million leaf comparisons. Built once per `items` array and cached on it
 * (the component's `items` is memoized on the fetched payload).
 */
interface TokenIndex {
  /** The catalog key the loader holds the row under now, or null. */
  held: Map<SkillPickerRow, string | null>
  /**
   * The key a row would be held under once usable: the held key, or for an
   * untrusted project row the key consent will put in the catalog. What a pick
   * inserts, so the consent step can insert a token that then resolves.
   */
  prospective: Map<SkillPickerRow, string | null>
  /** Lowercased leaf -> every row ending in it, in catalog order. */
  byLeaf: Map<string, SkillPickerRow[]>
}

const INDEX_CACHE = new WeakMap<readonly SkillPickerRow[], TokenIndex>()

function rowKey(row: SkillPickerRow): string {
  return row.key || row.name
}

function isDisplayKey(key: string): boolean {
  return DISPLAY_PREFIXES.some(p => key.startsWith(p))
}

/**
 * The key the loader's catalog holds each row under (catalog.py), or null when
 * the catalog does not hold it. An unprefixed row IS a catalog row. A prefixed
 * row the listing collapsed with a loader spelling carries that spelling in
 * `aliases` (`extra_paths` naming `~/.kiro/skills`). A `kiro-workspace/<rel>`
 * project row is held under `<rel>` once its project is trusted (an untrusted
 * one is listed for the consent step but is not in the catalog), unless
 * another row is already held under `<rel>`: the loader skips a project copy
 * behind a same-named global skill, whatever spelling the listing kept for
 * that skill. An unaliased `kiro-user/` or `package/` row is not in the
 * catalog (the loader walks neither `~/.kiro/skills` nor an edition root on
 * its own), though an agent's skill:// mapping may still make its `$leaf`
 * resolve.
 */
function buildIndex(items: readonly SkillPickerRow[]): TokenIndex {
  const held = new Map<SkillPickerRow, string | null>()
  const prospective = new Map<SkillPickerRow, string | null>()
  const byLeaf = new Map<string, SkillPickerRow[]>()
  const heldKeys = new Set<string>()
  const projectRows: { row: SkillPickerRow; rel: string }[] = []
  for (const row of items) {
    const key = rowKey(row)
    const leaf = leafOf(key).toLowerCase()
    const group = byLeaf.get(leaf)
    if (group) group.push(row); else byLeaf.set(leaf, [row])
    let k: string | null = null
    if (!isDisplayKey(key)) {
      k = key
    } else {
      const alias = (row.aliases ?? []).find(a => a && !isDisplayKey(a))
      if (alias) k = alias
      else if (key.startsWith(WORKSPACE_PREFIX)) {
        // Decided after every other row, since any of them may hold `rel`.
        projectRows.push({ row, rel: key.slice(WORKSPACE_PREFIX.length) })
        continue
      }
    }
    held.set(row, k)
    prospective.set(row, k)
    if (k !== null) heldKeys.add(k)
  }
  for (const { row, rel } of projectRows) {
    const free = !heldKeys.has(rel)
    // Untrusted: listed for the consent step, not in the catalog until then.
    held.set(row, free && row.trusted !== false ? rel : null)
    prospective.set(row, free ? rel : null)
  }
  return { held, prospective, byLeaf }
}

function indexOf(items: readonly SkillPickerRow[]): TokenIndex {
  let index = INDEX_CACHE.get(items)
  if (!index) {
    index = buildIndex(items)
    INDEX_CACHE.set(items, index)
  }
  return index
}

/** The key the loader's catalog holds `row` under, or null (see buildIndex). */
export function loaderKey(row: SkillPickerRow, items: readonly SkillPickerRow[]): string | null {
  const index = indexOf(items)
  // A row not in `items` (a caller's probe) is judged as if appended to it.
  return index.held.has(row) ? index.held.get(row)! : buildIndex([...items, row]).held.get(row)!
}

function qualifiedToken(row: SkillPickerRow, index: TokenIndex): string | null {
  const k = index.prospective.get(row) ?? null
  return k !== null && DOLLAR_TOKEN.test(k) ? k : null
}

/**
 * The text to insert after `$` for `row`, or null when no token the picker
 * could insert is guaranteed to load THIS row's body. The last folder while
 * no OTHER catalog-held row ends in it: the form the picker always inserted,
 * and the only one a row outside the catalog can use (an unaliased native
 * skill an agent mapping makes resolvable). Rows outside the catalog cannot
 * make a leaf ambiguous, since the backend never sees them. Under a shared
 * leaf a bare `$leaf` resolves to nothing or to the other skill, so the token
 * is the catalog key, which the backend matches exactly; a row with none, or
 * whose key the `$` grammar cannot carry (an uppercase folder, a package `:`
 * qualifier), gets null. rankSkills hides a null-token row behind its
 * sibling, as the old leaf dedupe did. When no sibling can show either (two
 * edition roots shipping the same path, `package/<q1>:x` and `package/<q2>:x`),
 * the first row keeps the bare leaf so the skill does not vanish: that is the
 * row the old dedupe kept, and the agent's mapping picks the copy.
 */
export function dollarToken(row: SkillPickerRow, items: readonly SkillPickerRow[]): string | null {
  const index = indexOf(items)
  if (!index.held.has(row)) return dollarToken(row, [...items, row])
  const key = rowKey(row)
  const leaf = leafOf(key)
  // A second row with the same key is the same skill listed twice (rankSkills
  // collapses it), not a sibling sharing the leaf.
  const sameLeaf = index.byLeaf.get(leaf.toLowerCase()) ?? [row]
  const others = sameLeaf.filter(o => rowKey(o) !== key)
  // Only a catalog-held sibling makes a bare `$leaf` ambiguous; the backend
  // never sees the others.
  if (!others.some(o => index.held.get(o) !== null)) {
    // A held row owns its leaf outright. Rows the catalog does not hold still
    // crowd a leaf among themselves (two qualified package copies, two
    // untrusted project groups): each inserts its prospective key when it has
    // one; otherwise the first keeps the bare leaf and the rest hide.
    if (index.held.get(row) !== null) return leaf
    if (others.length === 0) return leaf
    const own = qualifiedToken(row, index)
    if (own !== null) return own
    return sameLeaf[0] === row ? leaf : null
  }
  const own = qualifiedToken(row, index)
  if (own !== null) return own
  // No qualified token here. If no same-leaf row has one, nothing would list
  // for this leaf; keep the first row under the bare leaf, as the old dedupe did.
  if (sameLeaf.some(o => qualifiedToken(o, index) !== null)) return null
  return sameLeaf[0] === row ? leaf : null
}

/**
 * Filter `items` by `query` and order them best match first. An empty query
 * lists every row in catalog order. Rows that share a key (the same skill
 * listed twice) collapse to the first. Rows that merely share a last folder
 * both list when each has its own resolvable token (dollarToken); a
 * shared-leaf row with none is hidden, since any token the picker inserted
 * for it would load another skill's body or nothing.
 */
export function rankSkills<T extends SkillPickerRow>(items: readonly T[], query: string): T[] {
  const q = query.toLowerCase()
  const seen = new Set<string>()
  const ranked: { item: T; tier: number; score: number; leaf: string; order: number }[] = []
  for (const s of items) {
    const key = s.key || s.name
    if (seen.has(key) || dollarToken(s, items) === null) continue
    const hit = q ? matchTier(s, q) : { tier: TIER_EXACT, score: 0 }
    if (hit === null) continue
    seen.add(key)
    ranked.push({ item: s, tier: hit.tier, score: hit.score, leaf: leafOf(key), order: ranked.length })
  }
  const byScore = makeScoreThenNameComparator<typeof ranked[number]>(r => r.score, r => r.leaf)
  ranked.sort((a, b) => {
    if (a.tier !== b.tier) return a.tier - b.tier
    if (a.tier === TIER_FUZZY) return byScore(a, b)
    return a.order - b.order
  })
  return ranked.map(r => r.item)
}
