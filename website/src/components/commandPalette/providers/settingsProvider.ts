import { createElement } from 'react'
import { useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { Settings } from 'lucide-react'
import type { NavigateFunction } from 'react-router-dom'

import { fuzzyMatch, makeScoreThenNameComparator } from '../../../utils/fuzzyMatch'
import type { ResourceProvider, Result } from '../types'
import { SETTINGS_REGISTRY } from '../settingsRegistry.gen'
import { SETTINGS_KEYWORDS } from '../settingsKeywords'
import type { SettingEntry } from '../settingsTypes'

/**
 * Settings provider for the Search Everywhere command palette.
 *
 * Backs the **Settings** tab. Searches over the codegen'd SETTINGS_REGISTRY
 * (label, description, keywords, tab name) using the shared fuzzy matcher.
 * Activation navigates to `/settings?tab=<tab>&highlight=<id>`.
 *
 * ## Tab filter syntax
 *
 * Prefix query with `<tab>:` to scope results to a single settings tab.
 * - Exact tab key: `voice: aws` — search within voice tab only.
 * - Unambiguous prefix: `disp: mode` → resolves to `display`.
 * - Empty remainder: `voice:` — lists all entries in that tab sorted by label.
 * - Ambiguous/unknown prefix: falls back to normal full-corpus search.
 *
 * Pure client-side, no API calls, participates in the All blend (instant).
 */

const PROVIDER_ID = 'settings'
const PROVIDER_LABEL = 'Settings'

function settingsIcon() {
  return createElement(Settings, { className: 'lucide-inline' })
}

const compareResults = makeScoreThenNameComparator<Result>(
  (r) => r.score,
  (r) => r.title,
)

/** Build searchable corpus for a setting entry (includes tab for unfiltered search). */
function buildCorpus(entry: SettingEntry): string {
  const parts = [entry.label]
  if (entry.description) parts.push(entry.description)
  // Tab name as searchable context
  parts.push(entry.tab)
  // Keywords
  const kws = SETTINGS_KEYWORDS[entry.id]
  if (kws) parts.push(...kws)
  return parts.join(' ')
}

/** Build corpus WITHOUT tab name — used for tab-filtered queries to avoid score distortion. */
function buildFilteredCorpus(entry: SettingEntry): string {
  const parts = [entry.label]
  if (entry.description) parts.push(entry.description)
  const kws = SETTINGS_KEYWORDS[entry.id]
  if (kws) parts.push(...kws)
  return parts.join(' ')
}

/** Tab-prefix filter regex: `tabname:` optionally followed by a query. */
const TAB_FILTER_RE = /^([a-zA-Z]+):\s*(.*)$/

/** Legacy tab keys that collapsed into the Channels tab (nav regroup).
 *  `slack: token` keeps working, scoped to the channels tab. */
const LEGACY_TAB_ALIASES: Record<string, string> = {
  slack: 'channels',
  discord: 'channels',
  telegram: 'channels',
  webex: 'channels',
  wecom: 'channels',
}

/**
 * Resolve a prefix string to a tab key from known tabs.
 * Returns the matched tab key, or null if ambiguous/unknown.
 *
 * Legacy alias keys participate in prefix matching too (mapped to their
 * targets and deduped), so muscle-memory shortcuts like `sl:` keep resolving
 * after the tab collapse — and `w:` resolves even though webex + wecom are
 * two aliases, because both map to the single channels target.
 */
export function resolveTabPrefix(prefix: string, tabKeys: string[]): string | null {
  const lower = prefix.toLowerCase()
  // Legacy per-channel tab keys scope to the collapsed channels tab.
  if (LEGACY_TAB_ALIASES[lower]) return LEGACY_TAB_ALIASES[lower]
  // Exact match first
  if (tabKeys.includes(lower)) return lower
  // Unambiguous prefix match over real tabs plus alias keys, deduped by
  // resolved target.
  const candidates = new Set<string>()
  for (const k of tabKeys) if (k.startsWith(lower)) candidates.add(k)
  for (const [alias, target] of Object.entries(LEGACY_TAB_ALIASES)) {
    if (alias.startsWith(lower)) candidates.add(target)
  }
  if (candidates.size === 1) return candidates.values().next().value ?? null
  // Ambiguous (2+) or unknown (0) — return null
  return null
}

function buildResult(entry: SettingEntry, score: number, indices: number[], navigate: NavigateFunction): Result {
  const tabLabel = entry.tab.charAt(0).toUpperCase() + entry.tab.slice(1)
  const subtitle = `${tabLabel} › ${entry.label}`
  // Extra params (e.g. channel=slack for the Channels list-detail tab) must
  // ride the deep link or the target panel never mounts and the highlight
  // silently no-ops.
  const extra = entry.params
    ? Object.entries(entry.params).map(([k, v]) => `&${encodeURIComponent(k)}=${encodeURIComponent(v)}`).join('')
    : ''
  const route = `/settings?tab=${entry.tab}${extra}&highlight=${encodeURIComponent(entry.id)}`
  return {
    id: `${PROVIDER_ID}:${entry.id}`,
    providerId: PROVIDER_ID,
    title: entry.label,
    subtitle,
    icon: settingsIcon(),
    score,
    indices,
    enter: { kind: 'navigate', route },
    onActivate: () => navigate(route),
  }
}

/**
 * Create a Settings provider bound to a router `navigate` function.
 * Pure (no hooks) — unit-testable with a stub navigate.
 */
export function createSettingsProvider(navigate: NavigateFunction): ResourceProvider {
  // Precompute tab keys from the registry
  const tabKeys = [...new Set(SETTINGS_REGISTRY.map((e) => e.tab))]

  return {
    id: PROVIDER_ID,
    label: PROVIDER_LABEL,
    icon: settingsIcon(),
    search(query: string): Result[] {
      const q = query.trim()
      if (q.length === 0) return []

      // Try tab-prefix filter
      const filterMatch = TAB_FILTER_RE.exec(q)
      if (filterMatch) {
        const [, prefix, remainder] = filterMatch
        const resolvedTab = resolveTabPrefix(prefix, tabKeys)
        if (resolvedTab) {
          return searchWithinTab(resolvedTab, remainder.trim(), navigate)
        }
        // Ambiguous or unknown — fall through to normal search
      }

      return searchFullCorpus(q, navigate)
    },
  }
}

/** Search within a single tab. Empty remainder lists all entries in that tab. */
function searchWithinTab(tab: string, remainder: string, navigate: NavigateFunction): Result[] {
  const tabEntries = SETTINGS_REGISTRY.filter((e) => e.tab === tab)

  if (remainder.length === 0) {
    // List all entries in this tab, sorted alphabetically by label
    return tabEntries
      .slice()
      .sort((a, b) => a.label.localeCompare(b.label))
      .map((entry) => buildResult(entry, 100, [], navigate))
  }

  const results: Result[] = []
  for (const entry of tabEntries) {
    const corpus = buildFilteredCorpus(entry)
    const corpusMatch = fuzzyMatch(remainder, corpus)
    if (!corpusMatch) continue

    const labelMatch = fuzzyMatch(remainder, entry.label)
    const score = labelMatch ? labelMatch.score : Math.max(1, Math.round(corpusMatch.score * 0.6))
    const indices = labelMatch ? labelMatch.indices : []

    results.push(buildResult(entry, score, indices, navigate))
  }

  results.sort(compareResults)
  return results
}

/** Normal full-corpus search (existing behavior). */
function searchFullCorpus(q: string, navigate: NavigateFunction): Result[] {
  const results: Result[] = []
  for (const entry of SETTINGS_REGISTRY) {
    const corpus = buildCorpus(entry)
    const corpusMatch = fuzzyMatch(q, corpus)
    if (!corpusMatch) continue

    const labelMatch = fuzzyMatch(q, entry.label)
    const score = labelMatch ? labelMatch.score : Math.max(1, Math.round(corpusMatch.score * 0.6))
    const indices = labelMatch ? labelMatch.indices : []

    results.push(buildResult(entry, score, indices, navigate))
  }

  results.sort(compareResults)
  return results
}

/**
 * React hook: a Settings provider wired to the app router.
 */
export function useSettingsProvider(): ResourceProvider {
  const navigate = useNavigate()
  return useMemo(() => createSettingsProvider(navigate), [navigate])
}
