/**
 * Governed settings entries: what a search may OFFER.
 *
 * `settingsRegistry.gen.ts` is codegen'd from the static settings tree, so it lists
 * every entry the build ships whether or not the running gateway offers it. The
 * Decisions (Jev) toggle is governed by `capabilities.decisions`: under a pin
 * `FeaturePreviewsSection` renders no card, and an unfiltered corpus would then hand
 * the user a search result that navigates to a section where the row is absent —
 * which reads as a broken page rather than a feature the fleet withdrew.
 *
 * Both search surfaces (the Search Everywhere palette provider and the in-page
 * Settings search) share one predicate, so they cannot drift into a state where one
 * offers what the other hides. These cases pin the predicate and the palette
 * provider's use of it; `decisionsCard.test.tsx` pins the card's own half.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { createElement, type ReactNode } from 'react'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import { api } from '../../api/client'
import type { ResourceProvider } from './types'

import { createSettingsProvider, useSettingsProvider } from './providers/settingsProvider'
import { SETTINGS_REGISTRY } from './settingsRegistry.gen'
import {
  DECISIONS_SETTING_ID,
  DECISIONS_SETTING_IDS,
  settingEntryOffered,
  type SettingsSearchGovernance,
} from './settingsSearchCore'
import { extractFromSource } from '../../../scripts/settingsExtract'
import type { SettingEntry } from './settingsTypes'

const decisionsEntry = (): SettingEntry => {
  const entry = SETTINGS_REGISTRY.find(e => e.id === DECISIONS_SETTING_ID)
  if (!entry) throw new Error(`${DECISIONS_SETTING_ID} missing from the registry`)
  return entry
}

/** Any OTHER entry, as the control: the predicate must gate one card, not the corpus. */
const otherEntry = (): SettingEntry => {
  const entry = SETTINGS_REGISTRY.find(e => !DECISIONS_SETTING_IDS.has(e.id))
  if (!entry) throw new Error('registry has only the Decisions entries')
  return entry
}

const ON: SettingsSearchGovernance = { decisionsEnabled: true }
const OFF: SettingsSearchGovernance = { decisionsEnabled: false }

describe('settingEntryOffered', () => {
  it('withholds the Decisions entry when the ceiling withdrew the feature', () => {
    expect(settingEntryOffered(decisionsEntry(), OFF)).toBe(false)
  })

  it('offers it when the ceiling permits', () => {
    expect(settingEntryOffered(decisionsEntry(), ON)).toBe(true)
  })

  it('gates that one entry and nothing else', () => {
    // The control. Without it, a predicate that returned `decisionsEnabled` for
    // EVERY entry would pass both cases above and empty the whole corpus under a pin.
    expect(settingEntryOffered(otherEntry(), OFF)).toBe(true)
    expect(settingEntryOffered(otherEntry(), ON)).toBe(true)
  })
})

describe('a read that did not succeed is not a denial', () => {
  // Driven through `useSettingsProvider`, not through a restatement of its own
  // expression: the first version of this block recomputed
  // `!isSuccess || data?.decisions_enabled === true` locally and asserted THAT, so
  // reverting the production line left it green. Mutation-checked now.
  const renderProvider = async (dashboardConfig: () => Promise<unknown>) => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.spyOn(api, 'dashboardConfig').mockImplementation(dashboardConfig as never)
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client }, createElement(MemoryRouter, null, children))
    const hook = renderHook(() => useSettingsProvider(), { wrapper })
    await waitFor(() => {
      expect(client.getQueryState(['dashboardConfig'])?.status).not.toBe('pending')
    })
    return hook
  }

  const offersDecisions = (provider: ResourceProvider) =>
    (provider.search('Decisions') as { id?: string }[]).some(r =>
      (r.id ?? '').includes(DECISIONS_SETTING_ID),
    )

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('offers the entry when the config read FAILED', async () => {
    // Nothing was denied — the dashboard does not know. Reporting the setting as absent
    // would be a stronger claim than it can make, and the card this leads to renders the
    // read-failed notice itself.
    const { result } = await renderProvider(() => Promise.reject(new Error('offline')))
    expect(offersDecisions(result.current)).toBe(true)
  })

  it('withholds it on a SUCCESSFUL read that says otherwise', async () => {
    const { result } = await renderProvider(() => Promise.resolve({ decisions_enabled: false }))
    expect(offersDecisions(result.current)).toBe(false)
  })

  it('withholds it on a successful read from a gateway that omits the field', async () => {
    // Answered, and the answer names no ceiling: there is nothing to honour and no card
    // to land on, so this is a definite "not offered" rather than an unknown.
    const { result } = await renderProvider(() => Promise.resolve({}))
    expect(offersDecisions(result.current)).toBe(false)
  })

  it('offers it on a successful read that permits', async () => {
    const { result } = await renderProvider(() => Promise.resolve({ decisions_enabled: true }))
    expect(offersDecisions(result.current)).toBe(true)
  })
})

describe('the palette provider honours it', () => {
  const nav = vi.fn()
  const ids = (results: { id?: string }[]) => results.map(r => r.id ?? '')
  const search = (g: SettingsSearchGovernance, q: string) =>
    createSettingsProvider(nav, g).search(q) as { id?: string }[]

  it('drops the entry from a full-corpus query under a pin', () => {
    // Typing its name is the obvious path to it.
    const permitted = ids(search(ON, 'Decisions'))
    expect(permitted.some(id => id.includes(DECISIONS_SETTING_ID))).toBe(true)
    const withdrawn = ids(search(OFF, 'Decisions'))
    expect(withdrawn.some(id => id.includes(DECISIONS_SETTING_ID))).toBe(false)
  })

  it('drops it from a tab LISTING too, where nobody typed its name', () => {
    // `developer:` with an empty remainder lists the tab wholesale. This is the path
    // that would surface a withdrawn entry unprompted, and it is a different code
    // branch from the corpus search above.
    const permitted = ids(search(ON, 'developer:'))
    expect(permitted.some(id => id.includes(DECISIONS_SETTING_ID))).toBe(true)
    const withdrawn = ids(search(OFF, 'developer:'))
    expect(withdrawn.some(id => id.includes(DECISIONS_SETTING_ID))).toBe(false)
    // The rest of the tab is still listed: the filter removed the card's rows, not
    // the tab. Counted against the governed set rather than a literal, so adding a
    // control to the card moves both sides of this assertion together.
    // The palette namespaces its result ids (`settings:<registry id>`), so membership
    // is tested on the suffix rather than on the whole string.
    const governedInTab = permitted.filter(id =>
      [...DECISIONS_SETTING_IDS].some(governed => id.endsWith(governed)),
    ).length
    expect(withdrawn.length).toBeGreaterThan(0)
    expect(governedInTab).toBeGreaterThan(1)
    expect(withdrawn.length).toBe(permitted.length - governedInTab)
  })

  it('drops every OTHER row the card owns, not only the one named Decisions', () => {
    // The leak this set exists to close: under a pin the card is not rendered at all,
    // so a hit on its credential row or its address field lands the reader on a
    // section where nothing is there. Searching for the ROW's own words is the path,
    // because nobody looking for an API key types "Decisions".
    const hasKeyRow = (results: string[]) =>
      results.some(id => id.endsWith('developer.jev-api-key'))
    expect(hasKeyRow(ids(search(ON, 'Jev API key')))).toBe(true)
    expect(hasKeyRow(ids(search(OFF, 'Jev API key')))).toBe(false)
  })
})

/**
 * The set and the card cannot drift.
 *
 * The registry carries no source-file field, so the governed ids are spelled out in
 * `settingsSearchCore`. This is what makes that list checkable rather than
 * aspirational: the real extractor reads `DecisionsCard.tsx`, and every label it
 * finds must resolve to a registry entry whose id is in the set. A control added to
 * the card without a line there fails here instead of leaking into a search under a
 * governance pin.
 */
describe('DECISIONS_SETTING_IDS covers the whole card', () => {
  it('names every entry the extractor finds in DecisionsCard.tsx', () => {
    const source = readFileSync(
      resolve(__dirname, '../../pages/settings/DecisionsCard.tsx'),
      'utf-8',
    )
    const { entries } = extractFromSource(source, 'DecisionsCard.tsx')
    // The extractor's own pass assigns no ids (that happens in the global pass), so
    // the entries are matched back by LABEL — which is also what a collision-suffixed
    // id would be matched by anyway.
    expect(entries.length).toBeGreaterThan(0)
    const missing: string[] = []
    for (const extracted of entries) {
      const real = SETTINGS_REGISTRY.find(
        e => e.tab === 'developer' && e.label === extracted.label,
      )
      if (!real) {
        missing.push(`${extracted.label}: no registry entry`)
      } else if (!DECISIONS_SETTING_IDS.has(real.id)) {
        missing.push(`${real.id}: not in DECISIONS_SETTING_IDS`)
      }
    }
    expect(missing, missing.join('\n')).toEqual([])
  })

  it('names no id the registry does not have, so a rename is caught', () => {
    const unknown = [...DECISIONS_SETTING_IDS].filter(
      id => !SETTINGS_REGISTRY.some(e => e.id === id),
    )
    expect(unknown, unknown.join('\n')).toEqual([])
  })
})
