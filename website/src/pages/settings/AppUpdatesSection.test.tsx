/**
 * Settings → About → Installed app updates (`app_updates.mode`) and the app page's per-app
 * override (`apps.<name>.auto_update`). The switch and each per-app pick PATCH
 * their own path; a stored value outside the vocabulary reads the way the gateway
 * reads it; the per-app Follow option names the section and the switch state it
 * follows; a refused save says so.
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
import { AppUpdatesSection, storedAppUpdateMode } from './AppUpdatesSection'
import AppAutoUpdateCard, { appAutoUpdatePath, storedAppAutoUpdate } from '../../components/appstore/AppAutoUpdateCard'

let qc: QueryClient
const wrap = (ui: React.ReactElement) => render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
type Cfg = { app_updates?: Record<string, unknown>; apps?: Record<string, Record<string, unknown>> }
let serverCfg: Cfg

const modeLabel = () => i18nT('pages.settings.appUpdates.mode_label')
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
  it('reads an unknown mode as off', () => {
    expect(storedAppUpdateMode(undefined)).toBe('off')
    expect(storedAppUpdateMode({ app_updates: { mode: 'ON' } })).toBe('off')
    // A value an older build wrote reads as off, as the gateway reads it.
    expect(storedAppUpdateMode({ app_updates: { mode: 'ask' } })).toBe('off')
    expect(storedAppUpdateMode({ app_updates: { mode: 'on' } })).toBe('on')
  })

  it('reads an unknown per-app value as Follow', () => {
    expect(appAutoUpdatePath('demo-app')).toBe('apps.demo-app.auto_update')
    expect(storedAppAutoUpdate({ apps: { a: { auto_update: 'sometimes' } } }, 'a')).toBe('')
    expect(storedAppAutoUpdate({ apps: { a: { auto_update: 'never' } } }, 'a')).toBe('never')
  })
})

/** The switch, once the saved config has loaded and it can be pressed. */
async function loadedModeSwitch() {
  const mode = await screen.findByRole('switch', { name: modeLabel() })
  await waitFor(() => expect(mode.getAttribute('aria-disabled')).toBeNull())
  return mode
}

describe('AppUpdatesSection', () => {
  it('is one switch, off by default, like the gateway row beside it', async () => {
    wrap(<AppUpdatesSection />)
    const mode = await loadedModeSwitch()
    expect(mode.getAttribute('aria-checked')).toBe('false')
    expect(screen.getAllByRole('switch')).toHaveLength(1)
    expect(screen.queryByRole('combobox')).toBeNull()
    expect(screen.getByText(i18nT('pages.settings.appUpdates.mode_hint'))).toBeTruthy()
  })

  it('reads a saved on as checked', async () => {
    serverCfg = { app_updates: { mode: 'on' } }
    wrap(<AppUpdatesSection />)
    const mode = await loadedModeSwitch()
    await waitFor(() => expect(mode.getAttribute('aria-checked')).toBe('true'))
  })

  it('is headed Installed app updates, not the desktop App updates', async () => {
    wrap(<AppUpdatesSection />)
    expect(await screen.findByRole('heading', { name: i18nT('pages.settings.appUpdates.title') })).toBeTruthy()
    expect(i18nT('pages.settings.appUpdates.title')).toBe('Installed app updates')
  })

  it('a flip writes app_updates.mode and shows at once', async () => {
    wrap(<AppUpdatesSection />)
    const mode = await loadedModeSwitch()
    fireEvent.click(mode)
    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledWith('app_updates.mode', 'on'))
    expect(mode.getAttribute('aria-checked')).toBe('true')
    fireEvent.click(mode)
    await waitFor(() => expect(api.patchConfig).toHaveBeenLastCalledWith('app_updates.mode', 'off'))
    expect(mode.getAttribute('aria-checked')).toBe('false')
  })

  it('says so when a save is refused', async () => {
    vi.mocked(api.patchConfig).mockRejectedValue(new Error('nope'))
    wrap(<AppUpdatesSection />)
    const mode = await loadedModeSwitch()
    fireEvent.click(mode)
    expect(await screen.findByText(i18nT('pages.settings.appUpdates.save_failed'))).toBeTruthy()
  })
})

describe('AppAutoUpdateCard', () => {
  it.each([
    ['on', 'pages.appDetailPage.auto_update_follow_on', 'Same as Installed app updates (on)'],
    ['off', 'pages.appDetailPage.auto_update_follow_off', 'Same as Installed app updates (off)'],
  ])('Follow names the section it follows and that switch state (%s)', async (mode, key, english) => {
    serverCfg = { app_updates: { mode } }
    wrap(<AppAutoUpdateCard name="demo-app" />)
    const select = await screen.findByRole('combobox', { name: overrideLabel() })
    // Loaded first: before the config arrives the stored mode reads off either way.
    await waitFor(() => expect(select).not.toBeDisabled())
    await waitFor(() => expect(select.querySelector('option')?.textContent).toBe(i18nT(key)))
    expect(i18nT(key)).toBe(english)
    expect(i18nT(key)).toContain(i18nT('pages.settings.appUpdates.title'))
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
