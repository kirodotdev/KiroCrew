/**
 * LTS edition: Feature Previews off, Debug tools hidden (RFC rfc-crew-lts-edition D12).
 *
 * An LTS gateway marks every preview path `lockedByPolicy` in the config schema and
 * lists `debug_tools` in `locked_controls` on the config read; the dashboard applies
 * both through `utils/editionLocks.ts`. Pinned here:
 * - every preview flag reads off, even when the browser cache says on, and every
 *   write is refused (nothing sent to the gateway);
 * - a key flipped in the browser (Developer > Storage, devtools) turns nothing on;
 * - the Feature Previews page keeps its sub-tab and its other cards but draws no
 *   preview card and no "See what it looks like" intro;
 * - Debug tools leaves the rail, its old link lands on the first page, and the
 *   scroll inspector stays off;
 * - Settings search offers no preview card;
 * - the oss edition (no locked path, no `locked_controls`) is unchanged.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, cleanup, render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'

import { api } from '../api/client'
import { DeveloperPanel } from '../pages/settings/DeveloperPanel'
import DeveloperPage from '../pages/DeveloperPage'
import LocalStorageDebug from '../pages/LocalStorageDebug'
import { usePreviewFlag } from '../hooks/usePreviewFlag'
import { applyConfigLocks } from '../hooks/usePreviewFlagServerSync'
import { INSPECTOR_KEYS, inspectorOn } from '../dev/scrollInspector'
import {
  PREVIEW_FLAGS, PREVIEW_WEBHOOKS, cachePreviewFlag, previewFlagConfigPath, readPreviewFlag, setPreviewFlag, setPreviewFlagPersister,
} from '../utils/previewFlags'
import { LOCKED_CONTROLS_CACHE_KEY, LOCKED_DEBUG_TOOLS, LOCKED_FEATURE_PREVIEWS, applyLockedControls, controlLocked } from '../utils/editionLocks'
import { PREVIEW_FLAG_SETTING_IDS, settingEntryOffered, type SettingsSearchGovernance } from '../components/commandPalette/settingsSearchCore'
import { SETTINGS_REGISTRY } from '../components/commandPalette/settingsRegistry.gen'
import { i18nT } from '../i18n/t'
import type { SchemaEntry } from '../components/settingRef/resolveSettingRef'

/** What an LTS gateway sends: `debug_tools` on the config read, and every
 *  preview path marked `lockedByPolicy` in the config schema. */
const LTS_BODY = { locked_controls: ['debug_tools'] }
const LTS_SCHEMA: ReadonlyMap<string, SchemaEntry> = new Map(PREVIEW_FLAGS.map(flag => {
  const path = previewFlagConfigPath(flag)
  return [path, { path, type: 'boolean', lockedByPolicy: true }] as const
}))
const OSS_SCHEMA: ReadonlyMap<string, SchemaEntry> = new Map(PREVIEW_FLAGS.map(flag => {
  const path = previewFlagConfigPath(flag)
  return [path, { path, type: 'boolean' }] as const
}))
const lockLts = () => applyConfigLocks(LTS_BODY, LTS_SCHEMA)
const lockOss = () => applyConfigLocks({}, OSS_SCHEMA)

function LocationProbe() {
  const loc = useLocation()
  return <div data-testid="loc">{loc.pathname + loc.search}</div>
}

/** Render the panel and let its config reads land inside act. */
async function renderPanel(path = '/settings/developer/feature-previews') {
  const view = render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={[path]}>
        <DeveloperPanel basePath="/settings" />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  return view
}

function FlagProbe({ flag }: { flag: string }) {
  return <div data-testid="flag">{usePreviewFlag(flag) ? 'on' : 'off'}</div>
}

const GOVERNANCE: SettingsSearchGovernance = {
  decisionsEnabled: true,
  tipsEnabled: true,
  updateSwitches: { app: true, gateway: true },
}

beforeEach(() => {
  localStorage.clear()
  applyLockedControls([])
  // Every read the page makes, stubbed: a real one cannot succeed under vitest.
  vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({} as never)
  vi.spyOn(api, 'dashboardConfig').mockResolvedValue({ decisions_enabled: true } as never)
  vi.spyOn(api, 'sharedCrewPreview').mockResolvedValue(null)
  vi.spyOn(api, 'getDecisionsConsent').mockResolvedValue({
    enabled: false, endpoint: '', configured_endpoint: 'https://api.typesafe.ai/v1/systemone', permits: false,
  })
})

afterEach(() => {
  // Unmount first: clearing the lock re-renders every mounted reader.
  cleanup()
  setPreviewFlagPersister(null)
  applyLockedControls([])
  localStorage.clear()
  vi.restoreAllMocks()
})

describe('LTS: Feature Previews off', () => {
  it('reads every flag off even when the browser cache says on', () => {
    for (const flag of PREVIEW_FLAGS) localStorage.setItem(flag, '1')
    lockLts()
    for (const flag of PREVIEW_FLAGS) expect(readPreviewFlag(flag)).toBe(false)
  })

  it('refuses every write and sends nothing to the gateway', () => {
    const persister = vi.fn()
    setPreviewFlagPersister(persister)
    lockLts()
    for (const flag of PREVIEW_FLAGS) {
      expect(setPreviewFlag(flag, true)).toBe(false)
      expect(cachePreviewFlag(flag, true)).toBe(false)
      expect(localStorage.getItem(flag)).toBeNull()
    }
    expect(persister).not.toHaveBeenCalled()
  })

  it('turns an open switch off the moment the lock arrives', () => {
    localStorage.setItem(PREVIEW_WEBHOOKS, '1')
    render(<FlagProbe flag={PREVIEW_WEBHOOKS} />)
    expect(screen.getByTestId('flag').textContent).toBe('on')
    act(() => lockLts())
    expect(screen.getByTestId('flag').textContent).toBe('off')
  })

  it('a key flipped in Developer > Storage or devtools turns nothing on', () => {
    lockLts()
    render(<FlagProbe flag={PREVIEW_WEBHOOKS} />)
    act(() => {
      localStorage.setItem(PREVIEW_WEBHOOKS, '1')
      window.dispatchEvent(new StorageEvent('storage', { key: PREVIEW_WEBHOOKS, newValue: '1' }))
    })
    expect(screen.getByTestId('flag').textContent).toBe('off')
    expect(readPreviewFlag(PREVIEW_WEBHOOKS)).toBe(false)
  })

  it('deleting the lock cache in Developer > Storage does not unlock the page', () => {
    lockLts()
    localStorage.setItem(PREVIEW_WEBHOOKS, '1')
    render(<LocalStorageDebug />)
    const row = screen.getByText(LOCKED_CONTROLS_CACHE_KEY).closest('[role="button"]') as HTMLElement
    fireEvent.click(within(row).getByRole('button', { name: i18nT('pages.localStorageDebug.delete_key') }))
    expect(localStorage.getItem(LOCKED_CONTROLS_CACHE_KEY)).toBeNull()
    expect(readPreviewFlag(PREVIEW_WEBHOOKS)).toBe(false)
  })

  it('keeps the sub-tab and its other cards, without a preview card or intro', async () => {
    localStorage.setItem(PREVIEW_WEBHOOKS, '1')
    lockLts()
    await renderPanel()
    expect(screen.getByRole('option', { name: 'Feature Previews' })).toBeInTheDocument()
    expect(await screen.findByRole('switch', { name: /automatic cards for all sessions/i })).toBeInTheDocument()
    expect(screen.queryByRole('switch', { name: 'Webhooks' })).toBeNull()
    expect(screen.queryByRole('switch', { name: 'Chat on a crew' })).toBeNull()
    expect(screen.queryByRole('switch', { name: 'Explore' })).toBeNull()
    expect(screen.queryByText('See what it looks like')).toBeNull()
    expect(screen.queryByText('Open Webhooks')).toBeNull()
  })

  it('clears a browser key still saying on, so a cleared lock cache shows nothing', () => {
    localStorage.setItem(PREVIEW_WEBHOOKS, '1')
    lockLts()
    expect(localStorage.getItem(PREVIEW_WEBHOOKS)).toBeNull()
  })

  it('lifting the lock puts each reader back on its own value', () => {
    lockLts()
    render(<FlagProbe flag={PREVIEW_WEBHOOKS} />)
    expect(screen.getByTestId('flag').textContent).toBe('off')
    // The same origin now answered by an oss gateway that holds the switch on.
    localStorage.setItem(PREVIEW_WEBHOOKS, '1')
    act(() => lockOss())
    expect(screen.getByTestId('flag').textContent).toBe('on')
  })

  it('reads the preview lock from the config schema, not the config body', () => {
    // The body alone (debug_tools only) does not lock previews.
    localStorage.setItem(PREVIEW_WEBHOOKS, '1')
    applyConfigLocks(LTS_BODY, OSS_SCHEMA)
    expect(readPreviewFlag(PREVIEW_WEBHOOKS)).toBe(true)
    applyConfigLocks(LTS_BODY, LTS_SCHEMA)
    expect(readPreviewFlag(PREVIEW_WEBHOOKS)).toBe(false)
  })

  it('a read that has not answered yet keeps the lock the other read set', () => {
    lockLts()
    applyConfigLocks(undefined, OSS_SCHEMA)
    expect(controlLocked(LOCKED_DEBUG_TOOLS)).toBe(true)
    lockLts()
    applyConfigLocks({}, undefined)
    expect(controlLocked(LOCKED_FEATURE_PREVIEWS)).toBe(true)
    expect(controlLocked(LOCKED_DEBUG_TOOLS)).toBe(false)
  })

  it('Settings search offers no preview card', () => {
    // Every id names a real registry entry, so the list cannot drift silently.
    const ids = new Set(SETTINGS_REGISTRY.map(e => e.id))
    for (const id of PREVIEW_FLAG_SETTING_IDS) expect(ids.has(id)).toBe(true)
    const entries = SETTINGS_REGISTRY.filter(e => PREVIEW_FLAG_SETTING_IDS.has(e.id))
    lockLts()
    for (const entry of entries) expect(settingEntryOffered(entry, GOVERNANCE)).toBe(false)
  })
})

describe('LTS: Debug tools hidden', () => {
  it('leaves the rail; the other pages stay', async () => {
    lockLts()
    await renderPanel()
    const names = within(screen.getByRole('listbox', { name: 'Developer' })).getAllByRole('option').map(o => o.textContent?.trim())
    expect(names).toEqual(['Feature Previews', 'Logs', 'System', 'Telemetry', 'Storage', 'Memory', 'Config', 'Archive'])
  })

  it('its page path heals to the first page', async () => {
    lockLts()
    await renderPanel('/settings/developer/debug-tools')
    await waitFor(() => expect(screen.getByTestId('loc').textContent).toBe('/settings/developer'))
    expect(screen.queryByRole('switch', { name: /scroll inspector/i })).toBeNull()
  })

  it('the old /developer?tab=debug-tools link lands on the default page', async () => {
    lockLts()
    render(
      <MemoryRouter initialEntries={['/developer?tab=debug-tools']}>
        <Routes>
          <Route path="/developer" element={<DeveloperPage />} />
          <Route path="*" element={<LocationProbe />} />
        </Routes>
      </MemoryRouter>,
    )
    await waitFor(() => expect(screen.getByTestId('loc').textContent).not.toContain('/developer?'))
    expect(screen.getByTestId('loc').textContent).not.toContain('debug-tools')
  })

  it('the scroll inspector stays off even when its key says on', () => {
    lockLts()
    localStorage.setItem(INSPECTOR_KEYS.ENABLED_KEY, '1')
    window.dispatchEvent(new CustomEvent(INSPECTOR_KEYS.ENABLED_EVENT, { detail: true }))
    expect(inspectorOn()).toBe(false)
  })
})

describe('oss edition: unchanged', () => {
  it('a body without locked_controls locks nothing', () => {
    localStorage.setItem(PREVIEW_WEBHOOKS, '1')
    lockOss()
    expect(readPreviewFlag(PREVIEW_WEBHOOKS)).toBe(true)
    expect(setPreviewFlag(PREVIEW_WEBHOOKS, false)).toBe(true)
    expect(localStorage.getItem(LOCKED_CONTROLS_CACHE_KEY)).toBeNull()
  })

  it('lists Debug tools and draws the preview cards', async () => {
    lockOss()
    await renderPanel()
    expect(screen.getByRole('option', { name: 'Debug tools' })).toBeInTheDocument()
    expect(screen.getByRole('switch', { name: 'Webhooks' })).toBeInTheDocument()
  })

  it('the scroll inspector still follows its key', () => {
    localStorage.setItem(INSPECTOR_KEYS.ENABLED_KEY, '1')
    window.dispatchEvent(new CustomEvent(INSPECTOR_KEYS.ENABLED_EVENT, { detail: true }))
    expect(inspectorOn()).toBe(true)
    window.dispatchEvent(new CustomEvent(INSPECTOR_KEYS.ENABLED_EVENT, { detail: false }))
  })
})
