/**
 * Settings → Chat → Model → Apps: one model picker per enabled app, writing
 * `apps.<name>.model`. Inherit (`''`) is the default and the first option; a pick
 * PATCHes that app's path and nothing else; a retained pin the account stopped
 * advertising stays visible instead of reading as Inherit; a refused save says so.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

vi.mock('../../api/client', () => ({
  api: {
    kirocrewConfig: vi.fn(),
    patchConfig: vi.fn(),
    listApps: vi.fn(),
  },
}))

vi.mock('../../hooks/useAvailableModels', () => ({
  useAvailableModelsQuery: () => ({
    data: [{ name: 'auto' }, { name: 'claude-haiku-5' }, { name: 'claude-opus-5' }],
    isSuccess: true,
    isDegraded: false,
  }),
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
  AppModelsSection,
  appModelOptions,
  appModelPath,
  appsForModelRows,
  inheritOptionLabel,
  storedAppModel,
} from './AppModelsSection'
import type { InstalledApp } from '../../components/appstore/types'

const app = (name: string, displayName: string, enabled = true) =>
  ({ name, displayName, enabled, version: '1.0.0', installedAt: '', manifest: { name, version: '1.0.0', displayName, description: '', author: '' } }) as InstalledApp

let qc: QueryClient
const wrap = (ui: React.ReactElement) => render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
// The server's config: a PATCH lands in it and every read returns it.
let serverCfg: { apps?: Record<string, { model?: unknown }>; agent?: { model?: string } }

beforeEach(() => {
  vi.clearAllMocks()
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  serverCfg = {}
  vi.mocked(api.kirocrewConfig).mockImplementation(async () => structuredClone(serverCfg))
  vi.mocked(api.patchConfig).mockImplementation(async (path: string, value: unknown) => {
    const [, name] = path.split('.')
    serverCfg.apps = { ...serverCfg.apps, [name]: { model: value } }
    return { ok: true }
  })
  vi.mocked(api.listApps).mockResolvedValue([
    app('zeta-app', 'Zeta'),
    app('alpha-app', 'Alpha'),
    app('off-app', 'Off', false),
  ])
})

describe('helpers', () => {
  it('names what Inherit falls back to: the Default Model, or auto when unset', () => {
    expect(inheritOptionLabel('claude-opus-5')).toBe(
      i18nT('pages.settings.appModels.inherit_named', { model: 'claude-opus-5' }),
    )
    expect(inheritOptionLabel('claude-opus-5')).toContain('claude-opus-5')
    for (const unset of ['auto', '', undefined, 5]) {
      expect(inheritOptionLabel(unset)).toBe(i18nT('pages.settings.appModels.inherit_auto'))
    }
  })

  it('names the per-app path', () => {
    expect(appModelPath('demo-app')).toBe('apps.demo-app.model')
  })

  it('reads both inherit spellings and a non-string as empty', () => {
    expect(storedAppModel(undefined, 'a')).toBe('')
    expect(storedAppModel({ a: { model: 'auto' } }, 'a')).toBe('')
    expect(storedAppModel({ a: { model: 5 } }, 'a')).toBe('')
    expect(storedAppModel({ a: { model: ' claude-haiku-5 ' } }, 'a')).toBe('claude-haiku-5')
  })

  it('offers Inherit first, drops auto, and keeps a pin the list lost', () => {
    expect(appModelOptions(['auto', 'm1'], [''])).toEqual(['', 'm1'])
    expect(appModelOptions(['auto', 'm1'], ['gone', 'gone'])).toEqual(['', 'm1', 'gone'])
  })

  it('lists enabled apps only, by display name', () => {
    const rows = appsForModelRows([app('z', 'Zeta'), app('a', 'Alpha'), app('o', 'Off', false)])
    expect(rows.map(r => r.name)).toEqual(['a', 'z'])
  })
})

describe('AppModelsSection', () => {
  it('shows one Inherit row per enabled app by default', async () => {
    wrap(<AppModelsSection />)
    const alpha = await screen.findByRole('combobox', { name: 'Alpha' })
    const zeta = screen.getByRole('combobox', { name: 'Zeta' })
    expect(screen.queryByRole('combobox', { name: 'Off' })).toBeNull()
    await waitFor(() => expect(alpha).not.toBeDisabled())
    expect((alpha as HTMLSelectElement).value).toBe('')
    expect((zeta as HTMLSelectElement).value).toBe('')
    expect(alpha.querySelector('option')?.textContent).toBe(i18nT('pages.settings.appModels.inherit_auto'))
    expect(Array.from(alpha.querySelectorAll('option')).map(o => o.getAttribute('value'))).toEqual(['', 'claude-haiku-5', 'claude-opus-5'])
  })

  it('the Inherit option names the Default Model it falls back to', async () => {
    serverCfg = { agent: { model: 'claude-opus-5' } }
    wrap(<AppModelsSection />)
    const alpha = await screen.findByRole('combobox', { name: 'Alpha' })
    await waitFor(() =>
      expect(alpha.querySelector('option')?.textContent).toBe(
        i18nT('pages.settings.appModels.inherit_named', { model: 'claude-opus-5' }),
      ),
    )
  })

  it('a pick writes that app path and shows at once', async () => {
    wrap(<AppModelsSection />)
    const alpha = await screen.findByRole('combobox', { name: 'Alpha' })
    await waitFor(() => expect(alpha).not.toBeDisabled())
    fireEvent.change(alpha, { target: { value: 'claude-haiku-5' } })
    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledWith('apps.alpha-app.model', 'claude-haiku-5'))
    expect(api.patchConfig).toHaveBeenCalledTimes(1)
    expect((alpha as HTMLSelectElement).value).toBe('claude-haiku-5')
  })

  it('choosing Inherit writes the empty value', async () => {
    serverCfg = { apps: { 'alpha-app': { model: 'claude-opus-5' } } }
    wrap(<AppModelsSection />)
    const alpha = await screen.findByRole('combobox', { name: 'Alpha' })
    await waitFor(() => expect((alpha as HTMLSelectElement).value).toBe('claude-opus-5'))
    fireEvent.change(alpha, { target: { value: '' } })
    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledWith('apps.alpha-app.model', ''))
  })

  it('a retained pin the account no longer advertises stays shown', async () => {
    serverCfg = { apps: { 'zeta-app': { model: 'retired-model' } } }
    wrap(<AppModelsSection />)
    const zeta = await screen.findByRole('combobox', { name: 'Zeta' })
    await waitFor(() => expect((zeta as HTMLSelectElement).value).toBe('retired-model'))
  })

  it('a refused save is reported', async () => {
    vi.mocked(api.patchConfig).mockRejectedValue(new Error('not available on your account'))
    wrap(<AppModelsSection />)
    const alpha = await screen.findByRole('combobox', { name: 'Alpha' })
    await waitFor(() => expect(alpha).not.toBeDisabled())
    fireEvent.change(alpha, { target: { value: 'claude-opus-5' } })
    expect(await screen.findByText(i18nT('pages.settings.appModels.failed_to_save', { app: 'Alpha' }))).toBeInTheDocument()
  })

  it('says so when no app is enabled', async () => {
    vi.mocked(api.listApps).mockResolvedValue([app('off-app', 'Off', false)])
    wrap(<AppModelsSection />)
    expect(await screen.findByText(i18nT('pages.settings.appModels.none_enabled'))).toBeInTheDocument()
  })

  it('says so when the app list cannot be read', async () => {
    vi.mocked(api.listApps).mockRejectedValue(new Error('boom'))
    wrap(<AppModelsSection />)
    expect(await screen.findByText(i18nT('pages.settings.appModels.load_failed'))).toBeInTheDocument()
  })
})
