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

/** Count apps per category, omitting empty categories, in canonical order. */
export function categoryCounts(apps: { tags?: unknown }[]): { category: Category; count: number }[] {
  const counts = new Map<Category, number>()
  for (const app of apps) {
    const c = categoryFor(app.tags)
    counts.set(c, (counts.get(c) || 0) + 1)
  }
  return CATEGORY_ORDER
    .filter(c => counts.has(c))
    .map(c => ({ category: c, count: counts.get(c)! }))
}
