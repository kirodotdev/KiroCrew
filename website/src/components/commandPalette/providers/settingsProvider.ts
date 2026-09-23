import { createElement } from 'react'
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { Settings } from 'lucide-react'
import type { NavigateFunction } from 'react-router-dom'

import { makeScoreThenNameComparator } from '../../../utils/fuzzyMatch'
import { i18nT } from '../../../i18n/t'
import { api } from '../../../api/client'
import type { ResourceProvider, Result } from '../types'
import { SETTINGS_REGISTRY } from '../settingsRegistry.gen'
import {
  localizedSettingLabel,
  scoreSettingEntry,
  settingEntryOffered,
  type SettingsSearchGovernance,
} from '../settingsSearchCore'
import { settingsRoute } from '../settingsRoute'
import { settingsTabLabel } from '../settingsTabLabel'
import type { SettingEntry } from '../settingsTypes'

/**
 * Settings provider for the Search Everywhere command palette.
 *
 * Backs the **Settings** tab. Searches over the codegen'd SETTINGS_REGISTRY
 * (label, description, keywords, tab name) using the shared fuzzy matcher.
 * Activation navigates to `/settings/<tab>?highlight=<id>` (second-level
 * selections ride as the second path segment).
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
/**
 * Catalog KEY for the palette tab label, not the string: a literal here would be
 * frozen at module load and never re-resolve on a language switch. `i18nT()` is
 * called where the provider object is built, inside `createSettingsProvider()` —
 * `useSettingsProvider`'s `useMemo` is destroyed when `<App>` remounts on the
 * language key (see `src/i18n/t.ts`), so that call site does re-run.
 *
 * Reuses `nav.settings` ('Settings') rather than minting a duplicate: it is the same
 * word for the same destination as the sidebar nav entry.
 */
const PROVIDER_LABEL_KEY = 'nav.settings'

function settingsIcon() {
  return createElement(Settings, { className: 'lucide-inline' })
}

const compareResults = makeScoreThenNameComparator<Result>(
  (r) => r.score,
  (r) => r.title,
)

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

function buildResult(
  entry: SettingEntry,
  score: number,
  indices: number[],
  navigate: NavigateFunction,
  displayLabel: string,
): Result {
  // Capitalizing the tab key produced "Computer-use" for `computer-use`, and in any
  // non-English locale it rendered the English machine key for every tab. The shared
  // resolver reads the same catalog label the settings page shows.
  //
  // `displayLabel` is the shared scorer's `localizedLabel` (fan-out suffix
  // re-appended): the palette shows the SAME row text as the in-page box, and
  // the score's highlight indices point into this string — rendering
  // `entry.label` here would localize the corpus but display English with
  // misaligned highlights for non-English users.
  const subtitle = `${settingsTabLabel(entry.tab)} › ${displayLabel}`
  const route = settingsRoute(entry)
  return {
    id: `${PROVIDER_ID}:${entry.id}`,
    providerId: PROVIDER_ID,
    title: displayLabel,
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
 *
 * *governance* carries the answers that decide whether an entry may be offered at
 * all. It is passed in rather than read here because `search` is SYNCHRONOUS: the
 * hook below subscribes to the answer and rebuilds the provider when it lands.
 */
export function createSettingsProvider(
  navigate: NavigateFunction,
  governance: SettingsSearchGovernance,
): ResourceProvider {
  // Tab keys stay derived from the FULL registry: they name the tabs a query may
  // scope to, and a governed entry does not remove its tab (the developer tab has
  // many others). Only the entries themselves are filtered.
  const tabKeys = [...new Set(SETTINGS_REGISTRY.map((e) => e.tab))]

  return {
    id: PROVIDER_ID,
    // A GETTER, not a plain call: the provider object is built inside a `useMemo`
    // whose deps do not include the language, so `label: i18nT(...)` would resolve
    // once and keep the pre-switch wording forever. `LanguageProvider` forces a
    // re-RENDER via `cloneElement` (it deliberately does NOT remount — see its own
    // comment rejecting `key={active}`), and a re-render does not recompute a memo.
    // An accessor moves the lookup to the consumer's render, where the tab strip
    // reads it. Satisfies `ResourceProvider.label: string`.
    get label() { return i18nT(PROVIDER_LABEL_KEY) },
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
          return searchWithinTab(resolvedTab, remainder.trim(), navigate, governance)
        }
        // Ambiguous or unknown — fall through to normal search
      }

      return searchFullCorpus(q, navigate, governance)
    },
  }
}

/** Search within a single tab. Empty remainder lists all entries in that tab. */
function searchWithinTab(
  tab: string,
  remainder: string,
  navigate: NavigateFunction,
  governance: SettingsSearchGovernance,
): Result[] {
  // Filtered here as well as in the full corpus: the empty-remainder branch below
  // LISTS a tab wholesale, which is the one path that would surface a withdrawn
  // entry without anyone typing its name.
  const tabEntries = SETTINGS_REGISTRY.filter(
    (e) => e.tab === tab && settingEntryOffered(e, governance),
  )

  if (remainder.length === 0) {
    // List all entries in this tab, sorted alphabetically by the DISPLAYED
    // (localized) label so the order matches what the user reads.
    return tabEntries
      .map((entry) => ({ entry, label: localizedSettingLabel(entry) }))
      .sort((a, b) => a.label.localeCompare(b.label))
      .map(({ entry, label }) => buildResult(entry, 100, [], navigate, label))
  }

  const results: Result[] = []
  for (const entry of tabEntries) {
    // Tab excluded from the corpus: within a tab-scoped query the (constant)
    // tab name would only distort ranking.
    const s = scoreSettingEntry(remainder, entry, { includeTab: false })
    if (!s) continue
    results.push(buildResult(entry, s.score, s.indices, navigate, s.localizedLabel))
  }

  results.sort(compareResults)
  return results
}

/** Normal full-corpus search (existing behavior). */
function searchFullCorpus(
  q: string,
  navigate: NavigateFunction,
  governance: SettingsSearchGovernance,
): Result[] {
  const results: Result[] = []
  for (const entry of SETTINGS_REGISTRY) {
    if (!settingEntryOffered(entry, governance)) continue
    const s = scoreSettingEntry(q, entry)
    if (!s) continue
    results.push(buildResult(entry, s.score, s.indices, navigate, s.localizedLabel))
  }

  results.sort(compareResults)
  return results
}

/**
 * React hook: a Settings provider wired to the app router.
 *
 * Subscribes to the SAME `['dashboardConfig']` query the Decisions card reads, so
 * the search and the card cannot disagree about whether the feature exists. Shared
 * key and shared stale window mean this adds no request of its own on a dashboard
 * that has already loaded.
 */
export function useSettingsProvider(): ResourceProvider {
  const navigate = useNavigate()
  const dashCfgQ = useQuery<{ decisions_enabled?: boolean }>({
    queryKey: ['dashboardConfig'],
    queryFn: () => api.dashboardConfig(),
    staleTime: 30_000,
  })
  // Offer unless the read SUCCEEDED and said otherwise: a failed or in-flight read
  // is not a denial, and the card this navigates to reports the failure itself.
  const decisionsEnabled = !dashCfgQ.isSuccess || dashCfgQ.data?.decisions_enabled === true
  return useMemo(
    () => createSettingsProvider(navigate, { decisionsEnabled }),
    [navigate, decisionsEnabled],
  )
}
