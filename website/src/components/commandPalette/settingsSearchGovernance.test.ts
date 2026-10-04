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
import { Provider } from 'react-redux'

import { api } from '../../api/client'
import { createTestStore } from '../../test/helpers'
import { updateInfoQuery } from '../../api/updateInfoQuery'
import { useUpdateSubscription } from '../../hooks/useUpdateSubscription'
import { sseStatus } from '../../store/dashboardSlice'
import type { ResourceProvider } from './types'

import { createSettingsProvider, useSettingsProvider } from './providers/settingsProvider'
import { SETTINGS_REGISTRY } from './settingsRegistry.gen'
import {
  APP_AUTO_DOWNLOAD_SETTING_ID,
  DECISIONS_SETTING_ID,
  DECISIONS_SETTING_IDS,
  FEATURE_TIPS_SETTING_ID,
  GATEWAY_AUTO_UPDATE_SETTING_ID,
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

const entryById = (id: string): SettingEntry => {
  const entry = SETTINGS_REGISTRY.find(e => e.id === id)
  if (!entry) throw new Error(`${id} missing from the registry`)
  return entry
}

const tipsEntry = (): SettingEntry => {
  const entry = SETTINGS_REGISTRY.find(e => e.id === FEATURE_TIPS_SETTING_ID)
  if (!entry) throw new Error(`${FEATURE_TIPS_SETTING_ID} missing from the registry`)
  return entry
}

/** Every id some governance answer can withhold. */
const GOVERNED_IDS = new Set([
  ...DECISIONS_SETTING_IDS, FEATURE_TIPS_SETTING_ID, GATEWAY_AUTO_UPDATE_SETTING_ID, APP_AUTO_DOWNLOAD_SETTING_ID,
])

/** Any OTHER entry, as the control: the predicate must gate one card, not the corpus. */
const otherEntry = (): SettingEntry => {
  const entry = SETTINGS_REGISTRY.find(e => !GOVERNED_IDS.has(e.id))
  if (!entry) throw new Error('registry has only the governed entries')
  return entry
}

const BOTH_SWITCHES = { app: true, gateway: true }
const ON: SettingsSearchGovernance = { decisionsEnabled: true, tipsEnabled: true, updateSwitches: BOTH_SWITCHES }
const OFF: SettingsSearchGovernance = { ...ON, decisionsEnabled: false }
const TIPS_OFF: SettingsSearchGovernance = { ...ON, tipsEnabled: false }

describe('the governed ids', () => {
  it('all name a row the registry has, so a relabel cannot silently re-offer it', () => {
    const ids = new Set(SETTINGS_REGISTRY.map(e => e.id))
    expect([...GOVERNED_IDS].filter(id => !ids.has(id))).toEqual([])
  })
})

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

  it('withholds the Feature Tips entry when the instance config turned tips off', () => {
    // With tips off the Chat rail can drop its Discovery group entirely, and the
    // sub-nav self-heals `sub=discovery` to the first group -- a hit would land the
    // reader on a page without the toggle.
    expect(settingEntryOffered(tipsEntry(), TIPS_OFF)).toBe(false)
    expect(settingEntryOffered(tipsEntry(), ON)).toBe(true)
  })

  it('the two governed answers do not leak into each other', () => {
    expect(settingEntryOffered(tipsEntry(), OFF)).toBe(true)
    expect(settingEntryOffered(decisionsEntry(), TIPS_OFF)).toBe(true)
    expect(settingEntryOffered(otherEntry(), TIPS_OFF)).toBe(true)
  })

  it('offers each About update switch only where About draws it', () => {
    const app = entryById(APP_AUTO_DOWNLOAD_SETTING_ID)
    const gateway = entryById(GATEWAY_AUTO_UPDATE_SETTING_ID)
    const only = (updateSwitches: SettingsSearchGovernance['updateSwitches']) => ({ ...ON, updateSwitches })
    expect(settingEntryOffered(app, only({ app: true, gateway: false }))).toBe(true)
    expect(settingEntryOffered(gateway, only({ app: true, gateway: false }))).toBe(false)
    expect(settingEntryOffered(app, only({ app: false, gateway: true }))).toBe(false)
    expect(settingEntryOffered(gateway, only({ app: false, gateway: true }))).toBe(true)
    expect(settingEntryOffered(otherEntry(), only({ app: false, gateway: false }))).toBe(true)
  })
})

describe('a read that did not succeed is not a denial', () => {
  // Driven through `useSettingsProvider`, not through a restatement of its own
  // expression: the first version of this block recomputed
  // `!isSuccess || data?.decisions_enabled === true` locally and asserted THAT, so
  // reverting the production line left it green. Mutation-checked now.
  const renderProvider = async (
    dashboardConfig: () => Promise<unknown>,
    tipsStatus: () => Promise<unknown> = () => Promise.resolve({ enabled_config: true }),
    autoEffect?: string,
    /** The updater's answer, as `useUpdateSubscription` seeds it at boot. */
    info?: Record<string, unknown>,
  ) => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    if (info) client.setQueryData(updateInfoQuery.queryKey, info)
    vi.spyOn(api, 'dashboardConfig').mockImplementation(dashboardConfig as never)
    vi.spyOn(api, 'tipsStatus').mockImplementation(tipsStatus as never)
    const store = createTestStore()
    store.dispatch(sseStatus({ uptime: '1m', sessions: 0, messages: 0, update_auto_effect: autoEffect } as never))
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(Provider, { store },
        createElement(QueryClientProvider, { client }, createElement(MemoryRouter, null, children)))
    const hook = renderHook(() => useSettingsProvider(), { wrapper })
    await waitFor(() => {
      expect(client.getQueryState(['dashboardConfig'])?.status).not.toBe('pending')
      expect(client.getQueryState(['tipsStatus'])?.status).not.toBe('pending')
    })
    return hook
  }

  const offersDecisions = (provider: ResourceProvider) =>
    (provider.search('Decisions') as { id?: string }[]).some(r =>
      (r.id ?? '').includes(DECISIONS_SETTING_ID),
    )

  const offersTips = (provider: ResourceProvider) =>
    (provider.search('Feature Tips') as { id?: string }[]).some(r =>
      (r.id ?? '').endsWith(FEATURE_TIPS_SETTING_ID),
    )

  afterEach(() => {
    vi.restoreAllMocks()
    delete window.updateAPI
  })

  const offers = (provider: ResourceProvider, id: string) =>
    (provider.search('auto-update') as { id?: string }[]).some(r => (r.id ?? '').endsWith(id))
  const ok = () => Promise.resolve({ decisions_enabled: true })
  const desktopShell = (over: Partial<UpdateAPI> = {}): UpdateAPI => ({
    onState: () => () => {},
    check: vi.fn().mockResolvedValue({ ok: true }),
    install: vi.fn().mockResolvedValue({ ok: true }),
    getInfo: vi.fn().mockResolvedValue({}),
    setAutoDownload: vi.fn().mockResolvedValue({ ok: true }),
    ...over,
  })

  // The rule's case table is `utils/updateSwitches.test.ts`; these prove the
  // search feeds it this window's bridge, the cached updater answer and the effect.
  it("offers the app switch, and not the app's own notify-only gateway, in a desktop window", async () => {
    window.updateAPI = desktopShell()
    const { result } = await renderProvider(ok, undefined, 'notify', { autoDownload: true })
    expect(offers(result.current, APP_AUTO_DOWNLOAD_SETTING_ID)).toBe(true)
    expect(offers(result.current, GATEWAY_AUTO_UPDATE_SETTING_ID)).toBe(false)
  })

  it("offers a self-updating gateway's switch, not the app's, where the cached answer says the updater is disabled", async () => {
    // A translocated or dev build attached to a cli.sh service: the gateway
    // still installs and restarts, so its switch is still the way to stop it.
    window.updateAPI = desktopShell()
    const { result } = await renderProvider(ok, undefined, 'install', { disabled: 'dev' })
    expect(offers(result.current, GATEWAY_AUTO_UPDATE_SETTING_ID)).toBe(true)
    expect(offers(result.current, APP_AUTO_DOWNLOAD_SETTING_ID)).toBe(false)
  })

  it('costs one getInfo() at startup, shared with the update subscription', async () => {
    const shell = desktopShell()
    window.updateAPI = shell
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.spyOn(api, 'dashboardConfig').mockResolvedValue({ decisions_enabled: true } as never)
    vi.spyOn(api, 'tipsStatus').mockResolvedValue({ enabled_config: true } as never)
    const store = createTestStore()
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(Provider, { store },
        createElement(QueryClientProvider, { client }, createElement(MemoryRouter, null, children)))
    renderHook(() => { useUpdateSubscription(); return useSettingsProvider() }, { wrapper })
    await waitFor(() => expect(client.getQueryState(updateInfoQuery.queryKey)?.status).toBe('success'))
    expect(shell.getInfo).toHaveBeenCalledTimes(1)
  })

  it('does not retry a failed boot read of the updater', async () => {
    // Best-effort: a rejection (old preload, IPC teardown) is left alone, even
    // under a client whose queries retry.
    const shell = desktopShell({ getInfo: vi.fn().mockRejectedValue(new Error('zzq')) })
    window.updateAPI = shell
    const client = new QueryClient({ defaultOptions: { queries: { retry: 1, retryDelay: 0 } } })
    const store = createTestStore()
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(Provider, { store },
        createElement(QueryClientProvider, { client }, createElement(MemoryRouter, null, children)))
    renderHook(() => useUpdateSubscription(), { wrapper })
    await waitFor(() => expect(client.getQueryState(updateInfoQuery.queryKey)?.status).toBe('error'))
    await new Promise(r => setTimeout(r, 20))
    expect(shell.getInfo).toHaveBeenCalledTimes(1)
  })

  it("reads the updater's answer from the cache only", async () => {
    // `useUpdateSubscription` reads getInfo() once at boot; a search must not add a second IPC.
    const shell = desktopShell()
    window.updateAPI = shell
    await renderProvider(ok, undefined, 'notify')
    expect(shell.getInfo).not.toHaveBeenCalled()
  })

  it('re-reads an answer over 30 s old when the search opens', async () => {
    // Tips turned off mid-session must not stay offered for the whole session.
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const old = Date.now() - 31_000
    client.setQueryData(['tipsStatus'], { enabled_config: true }, { updatedAt: old })
    client.setQueryData(['dashboardConfig'], { decisions_enabled: true }, { updatedAt: old })
    const tips = vi.spyOn(api, 'tipsStatus').mockResolvedValue({ enabled_config: false } as never)
    vi.spyOn(api, 'dashboardConfig').mockResolvedValue({ decisions_enabled: true } as never)
    const store = createTestStore()
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(Provider, { store },
        createElement(QueryClientProvider, { client }, createElement(MemoryRouter, null, children)))
    const { result } = renderHook(() => useSettingsProvider(), { wrapper })
    await waitFor(() => expect(offersTips(result.current)).toBe(false))
    expect(tips).toHaveBeenCalledTimes(1)
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

  it('withholds Feature Tips when the tips read SUCCEEDED with the config off', async () => {
    const { result } = await renderProvider(
      () => Promise.resolve({ decisions_enabled: true }),
      () => Promise.resolve({ enabled_config: false, opted_out: false }),
    )
    expect(offersTips(result.current)).toBe(false)
  })

  it('offers Feature Tips when the tips read FAILED', async () => {
    const { result } = await renderProvider(
      () => Promise.resolve({ decisions_enabled: true }),
      () => Promise.reject(new Error('offline')),
    )
    expect(offersTips(result.current)).toBe(true)
  })

  it('offers Feature Tips on a successful read with the config on', async () => {
    const { result } = await renderProvider(
      () => Promise.resolve({ decisions_enabled: true }),
      () => Promise.resolve({ enabled_config: true, opted_out: true }),
    )
    expect(offersTips(result.current)).toBe(true)
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

  it('drops Feature Tips from both the corpus query and the chat tab listing', () => {
    const hasTips = (results: string[]) => results.some(id => id.endsWith(FEATURE_TIPS_SETTING_ID))
    expect(hasTips(ids(search(ON, 'Feature Tips')))).toBe(true)
    expect(hasTips(ids(search(TIPS_OFF, 'Feature Tips')))).toBe(false)
    expect(hasTips(ids(search(ON, 'chat:')))).toBe(true)
    const listed = ids(search(TIPS_OFF, 'chat:'))
    expect(hasTips(listed)).toBe(false)
    expect(listed.length).toBe(ids(search(ON, 'chat:')).length - 1)
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
