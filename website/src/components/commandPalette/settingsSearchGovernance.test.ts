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
  settingEntryOffered,
  type SettingsSearchGovernance,
} from './settingsSearchCore'
import type { SettingEntry } from './settingsTypes'

const decisionsEntry = (): SettingEntry => {
  const entry = SETTINGS_REGISTRY.find(e => e.id === DECISIONS_SETTING_ID)
  if (!entry) throw new Error(`${DECISIONS_SETTING_ID} missing from the registry`)
  return entry
}

/** Any OTHER entry, as the control: the predicate must gate one id, not the corpus. */
const otherEntry = (): SettingEntry => {
  const entry = SETTINGS_REGISTRY.find(e => e.id !== DECISIONS_SETTING_ID)
  if (!entry) throw new Error('registry has only one entry')
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
    // The rest of the tab is still listed: the filter removed one row, not the tab.
    expect(withdrawn.length).toBeGreaterThan(0)
    expect(withdrawn.length).toBe(permitted.length - 1)
  })
})
