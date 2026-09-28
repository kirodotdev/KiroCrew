/**
 * Category derivation for Apps browsing.
 *
 * Apps carry free-form ``tags`` in their manifests; the store groups them
 * into a small set of canonical categories for the Discover rail. An app's
 * category is decided by checking categories in PRIORITY order against the
 * app's full tag set — the first category with any matching tag wins. The
 * most generic bucket (Productivity) is checked last so specific tags
 * (``oncall``, ``research``) always beat generic ones (``tasks``).
 */

import { i18nT } from '../../i18n/t'

export const CATEGORY_ORDER = [
  'Developer Tools',
  'Designer Tools',
  'On-call & Ops',
  'Productivity',
  'Agents & Automation',
  'Research & Writing',
  'Other',
] as const

export type Category = (typeof CATEGORY_ORDER)[number]

/**
 * Display label for each category — catalog KEYS, not strings.
 *
 * `CATEGORY_ORDER` above is the ID list, and it stays English and byte-stable: those
 * values are compared (`categoryFor(a.tags) !== category` in `AppsPage`), sorted with
 * `localeCompare`, and used as `Map` keys in `categoryCounts`. This table is the copy
 * half of that split.
 *
 * Keys, not strings: the module is evaluated once at import, so an `i18nT()` call in
 * the initializer would freeze the boot language and never re-resolve on a language
 * switch. The lookup happens in `categoryLabel()`, which runs during render. Shaped
 * as a flat `Record` of full literal keys and indexed inline at the `i18nT()` call,
 * because that is the form `scripts/check-i18n-keys.mjs` can resolve statically.
 */
export const CATEGORY_LABEL_KEY: Record<Category, string> = {
  'Developer Tools': 'components.appstore.categories.developer_tools',
  'On-call & Ops': 'components.appstore.categories.oncall_ops',
  'Productivity': 'components.appstore.categories.productivity',
  'Agents & Automation': 'components.appstore.categories.agents_automation',
  'Research & Writing': 'components.appstore.categories.research_writing',
  'Designer Tools': 'components.appstore.categories.designer_tools',
  'Other': 'components.appstore.categories.other',
}

/**
 * Localised display label for a category.
 *
 * Takes a plain `string` so a value read back from untrusted manifest data needs no
 * cast, and an id with no entry is returned VERBATIM rather than dressed up as copy —
 * same doctrine as `effortLabel()` in `lib/effort.ts`.
 */
export function categoryLabel(category: string): string {
  // `hasOwnProperty`, not `in`: an id of `toString` would otherwise resolve to an
  // inherited Object.prototype member and hand a function to i18next.
  return Object.prototype.hasOwnProperty.call(CATEGORY_LABEL_KEY, category)
    ? i18nT(CATEGORY_LABEL_KEY[category as Category])
    : category
}

/** Category → matching tags, in MATCH-priority order (specific → generic). */
const MATCHERS: [Category, Set<string>][] = [
  ['On-call & Ops', new Set(['oncall', 'operations', 'monitoring', 'tickets', 'pipelines'])],
  ['Research & Writing', new Set(['research', 'writing', 'docs'])],
  ['Designer Tools', new Set([
    'ux', 'critique', 'usability', 'heuristic-evaluation', 'designer-tools',
  ])],
  ['Developer Tools', new Set([
    'developer-tools', 'code-review', 'git', 'github', 'dev', 'worktrees',
    'pods', 'issue-triage', 'code-quality', 'open-source', 'performance',
  ])],
  ['Agents & Automation', new Set([
    'agents', 'automation', 'workflows', 'orchestration', 'autonomy',
    'autonudge', 'execution', 'collaboration', 'visualization',
  ])],
  ['Productivity', new Set([
    'productivity', 'tasks', 'inbox', 'slack', 'email', 'outlook', 'files',
    'explorer', 'aggregation', 'reports', 'team',
  ])],
]

/**
 * Derive the canonical category for an app from its manifest tags.
 *
 * Tags come from user-supplied external ``app.json`` files, so the shape is
 * untrusted: a non-array value, or non-string members, must not throw — this
 * runs during Discover's render, where a TypeError takes down the storefront.
 */
export function categoryFor(tags?: unknown): Category {
  const list = Array.isArray(tags) ? tags : []
  const set = new Set(
    list.filter((t): t is string => typeof t === 'string').map(t => t.toLowerCase()),
  )
  for (const [category, matches] of MATCHERS) {
    for (const tag of set) if (matches.has(tag)) return category
  }
  return 'Other'
}

/**
 * Published category id (catalog slug) → this client's category id.
 *
 * Written out rather than derived: the catalog publishes `oncall-ops` while this
 * client's id is `On-call & Ops`, which no mechanical slugify produces
 * (`on-call-ops`). A table fails loudly when a new published id has no home
 * here; a transform would silently invent one.
 *
 * Values are INDEXES into `CATEGORY_ORDER` rather than repeated string
 * literals. Spelling an id twice invites the two spellings to drift, and these
 * ids are compared byte-for-byte (`categoryFor(...) !== category`, `Map` keys),
 * so a drifted copy would not fail loudly -- it would quietly match nothing.
 *
 * The mapping is deliberately NOT total in either direction. `Designer Tools`
 * has no published counterpart, and a published id absent from this table is
 * DROPPED rather than shown -- the rail's copy comes from this client's i18n
 * catalog, so an unknown id could only render as a raw slug.
 */
export const PUBLISHED_CATEGORY_ID: Record<string, Category> = {
  'developer-tools': CATEGORY_ORDER[0],
  'designer-tools': CATEGORY_ORDER[1],
  'oncall-ops': CATEGORY_ORDER[2],
  'productivity': CATEGORY_ORDER[3],
  'agents-automation': CATEGORY_ORDER[4],
  'research-writing': CATEGORY_ORDER[5],
  'other': CATEGORY_ORDER[6],
}

/**
 * Merge a published rail order into the client's canonical order.
 *
 * The published document decides the relative order of the categories it NAMES.
 * A category it does not name keeps its position relative to its local
 * neighbours instead of being flushed to the end — otherwise a document that
 * simply forgot `Designer Tools` would silently demote it, which is a
 * presentation change nobody authored.
 *
 * Concretely: published ids are laid down in published order, and each unnamed
 * category is re-inserted after the last named category that precedes it
 * locally. An empty or unusable list returns the canonical order unchanged,
 * which is the answer the rail used before the document existed.
 */
export function mergeCategoryOrder(publishedIds: readonly string[]): Category[] {
  // `hasOwnProperty`, not `in`: a published id of `toString` would otherwise
  // resolve to an inherited Object.prototype member.
  const mapped: Category[] = []
  for (const id of publishedIds) {
    if (!Object.prototype.hasOwnProperty.call(PUBLISHED_CATEGORY_ID, id)) continue
    const category = PUBLISHED_CATEGORY_ID[id]
    if (!mapped.includes(category)) mapped.push(category)
  }
  if (mapped.length === 0) return [...CATEGORY_ORDER]

  const named = new Set(mapped)
  // Walk the canonical order once, collecting each unnamed category under the
  // named one it currently follows. `null` holds those that precede every named
  // category, so they stay at the front rather than jumping to the back.
  const trailing = new Map<Category | null, Category[]>()
  let anchor: Category | null = null
  for (const category of CATEGORY_ORDER) {
    if (named.has(category)) {
      anchor = category
      continue
    }
    const bucket = trailing.get(anchor)
    if (bucket) bucket.push(category)
    else trailing.set(anchor, [category])
  }

  const out: Category[] = [...(trailing.get(null) || [])]
  for (const category of mapped) {
    out.push(category)
    for (const extra of trailing.get(category) || []) out.push(extra)
  }
  return out
}

/** Count apps per category, omitting empty categories, in canonical order. */
export function categoryCounts(
  apps: { tags?: unknown }[],
  order: readonly Category[] = CATEGORY_ORDER,
): { category: Category; count: number }[] {
  const counts = new Map<Category, number>()
  for (const app of apps) {
    const c = categoryFor(app.tags)
    counts.set(c, (counts.get(c) || 0) + 1)
  }
  return order
    .filter(c => counts.has(c))
    .map(c => ({ category: c, count: counts.get(c)! }))
}
