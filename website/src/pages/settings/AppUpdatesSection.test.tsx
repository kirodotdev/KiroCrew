/**
 * Settings → About → App updates (`app_updates.*`) and the app page's per-app
 * override (`apps.<name>.auto_update`). Each pick PATCHes its own path; a stored
 * value outside the vocabulary reads the way the gateway reads it; the per-app
 * Follow option names the global mode it follows; a refused save says so.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

vi.mock('../../api/client', () => ({
  api: {
    kirocrewConfig: vi.fn(),
    patchConfig: vi.fn(),
  },
}))

// Native-select stand-in: the value and options are under test, not the Radix picker.
vi.mock('../../components/SimpleSelect', () => ({
  default: ({ options, optionLabels, value, onChange, disabled, 'aria-label': ariaLabel }: {
    options: string[]; optionLabels?: string[]; value: string; onChange: (v: string) => void
    disabled?: boolean; 'aria-label'?: string
  }) => (
    <select aria-label={ariaLabel} value={value} disabled={disabled} onChange={e => onChange(e.target.value)}>
      {options.map((o, i) => <option key={o} value={o}>{optionLabels?.[i] ?? o}</option>)}
    </select>
  ),
}))

import { api } from '../../api/client'
import { i18nT } from '../../i18n/t'
import {
  AppUpdatesSection,
  appUpdateModeLabel,
  storedAppUpdateInterval,
  storedAppUpdateMode,
} from './AppUpdatesSection'
import AppAutoUpdateCard, { appAutoUpdatePath, storedAppAutoUpdate } from '../../components/appstore/AppAutoUpdateCard'

let qc: QueryClient
const wrap = (ui: React.ReactElement) => render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
type Cfg = { app_updates?: Record<string, unknown>; apps?: Record<string, Record<string, unknown>> }
let serverCfg: Cfg

const modeLabel = () => i18nT('pages.settings.appUpdates.mode_label')
const intervalLabel = () => i18nT('pages.settings.appUpdates.interval_label')
const overrideLabel = () => i18nT('pages.appDetailPage.auto_update_label')

beforeEach(() => {
  vi.clearAllMocks()
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  serverCfg = {}
  vi.mocked(api.kirocrewConfig).mockImplementation(async () => structuredClone(serverCfg))
  vi.mocked(api.patchConfig).mockImplementation(async (path: string, value: unknown) => {
    const parts = path.split('.')
    if (parts[0] === 'app_updates') serverCfg.app_updates = { ...serverCfg.app_updates, [parts[1]]: value }
    else serverCfg.apps = { ...serverCfg.apps, [parts[1]]: { ...serverCfg.apps?.[parts[1]], [parts[2]]: value } }
    return { ok: true }
  })
})

describe('helpers', () => {
  it('reads an unknown mode as off and an unknown interval as six hours', () => {
    expect(storedAppUpdateMode(undefined)).toBe('off')
    expect(storedAppUpdateMode({ app_updates: { mode: 'ON' } })).toBe('off')
    expect(storedAppUpdateMode({ app_updates: { mode: 'ask' } })).toBe('ask')
    expect(storedAppUpdateInterval({ app_updates: { check_interval_hours: 0 } })).toBe(6)
    expect(storedAppUpdateInterval({ app_updates: { check_interval_hours: 12 } })).toBe(12)
  })

  it('reads an unknown per-app value as Follow', () => {
    expect(appAutoUpdatePath('demo-app')).toBe('apps.demo-app.auto_update')
    expect(storedAppAutoUpdate({ apps: { a: { auto_update: 'sometimes' } } }, 'a')).toBe('')
    expect(storedAppAutoUpdate({ apps: { a: { auto_update: 'never' } } }, 'a')).toBe('never')
  })
})

describe('AppUpdatesSection', () => {
  it('defaults to Off every six hours', async () => {
    wrap(<AppUpdatesSection />)
    const mode = await screen.findByRole('combobox', { name: modeLabel() })
    await waitFor(() => expect(mode).not.toBeDisabled())
    expect((mode as HTMLSelectElement).value).toBe('off')
    expect((screen.getByRole('combobox', { name: intervalLabel() }) as HTMLSelectElement).value).toBe('6')
  })

  it('a mode pick writes app_updates.mode and shows at once', async () => {
    wrap(<AppUpdatesSection />)
    const mode = await screen.findByRole('combobox', { name: modeLabel() })
    await waitFor(() => expect(mode).not.toBeDisabled())
    fireEvent.change(mode, { target: { value: 'on' } })
    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledWith('app_updates.mode', 'on'))
    expect((mode as HTMLSelectElement).value).toBe('on')
  })

  it('an interval pick writes a number', async () => {
    wrap(<AppUpdatesSection />)
    const interval = await screen.findByRole('combobox', { name: intervalLabel() })
    await waitFor(() => expect(interval).not.toBeDisabled())
    fireEvent.change(interval, { target: { value: '24' } })
    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledWith('app_updates.check_interval_hours', 24))
  })

  it('keeps a hand-set interval selectable', async () => {
    serverCfg = { app_updates: { check_interval_hours: 5 } }
    wrap(<AppUpdatesSection />)
    const interval = await screen.findByRole('combobox', { name: intervalLabel() })
    await waitFor(() => expect((interval as HTMLSelectElement).value).toBe('5'))
  })

  it('says so when a save is refused', async () => {
    vi.mocked(api.patchConfig).mockRejectedValue(new Error('nope'))
    wrap(<AppUpdatesSection />)
    const mode = await screen.findByRole('combobox', { name: modeLabel() })
    await waitFor(() => expect(mode).not.toBeDisabled())
    fireEvent.change(mode, { target: { value: 'ask' } })
    expect(await screen.findByText(i18nT('pages.settings.appUpdates.save_failed'))).toBeTruthy()
  })
})

describe('AppAutoUpdateCard', () => {
  it('Follow names the global mode it follows', async () => {
    serverCfg = { app_updates: { mode: 'ask' } }
    wrap(<AppAutoUpdateCard name="demo-app" />)
    const select = await screen.findByRole('combobox', { name: overrideLabel() })
    await waitFor(() =>
      expect(select.querySelector('option')?.textContent).toBe(
        i18nT('pages.appDetailPage.auto_update_follow', { mode: appUpdateModeLabel('ask') }),
      ),
    )
    expect((select as HTMLSelectElement).value).toBe('')
  })

  it('says so when the settings cannot be loaded', async () => {
    vi.mocked(api.kirocrewConfig).mockRejectedValue(new Error('down'))
    wrap(<AppAutoUpdateCard name="demo-app" />)
    expect(await screen.findByText(i18nT('pages.settings.appUpdates.load_failed'))).toBeTruthy()
  })

  it('a pick writes this app path only', async () => {
    wrap(<AppAutoUpdateCard name="demo-app" />)
    const select = await screen.findByRole('combobox', { name: overrideLabel() })
    await waitFor(() => expect(select).not.toBeDisabled())
    fireEvent.change(select, { target: { value: 'never' } })
    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledWith('apps.demo-app.auto_update', 'never'))
    expect(api.patchConfig).toHaveBeenCalledTimes(1)
  })
})
