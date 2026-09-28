/**
 * SourcesPopover — the Apps page gear popover. Registry management is its own
 * component (stubbed here); what belongs to THIS component is the
 * Install-from-Path escape hatch: the empty-path guard, the success sequence
 * (install → telemetry → cache invalidation → apps-changed broadcast →
 * onInstalled → close), the name fallback when the backend returns none, and
 * the failure path that reports through onError and leaves the popover open.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { i18nT } from '../i18n/t'

const mocks = vi.hoisted(() => ({ installApp: vi.fn() }))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (t, p: string) => (p in t ? t[p] : vi.fn().mockResolvedValue([])),
  }),
}))
const recordEventMock = vi.fn()
vi.mock('../rum', () => ({ recordEvent: (...a: unknown[]) => recordEventMock(...a) }))
// RegistryManager owns its own fetches and its own test file; stub it so the
// assertions here are about the popover's install flow only.
vi.mock('../components/RegistryManager', () => ({
  default: () => <div data-testid="zzq-registry-stub" />,
}))

import SourcesPopover from '../components/appstore/SourcesPopover'

const wrap = (ui: ReactNode) => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

const pathInput = () => screen.getByPlaceholderText(i18nT('components.appstore.sourcesPopover.path_to_app_directory'))
const installBtn = () => screen.getByRole('button', { name: i18nT('components.appstore.sourcesPopover.install') })

// NB: braces, not a concise arrow body — `mockResolvedValue` RETURNS the mock,
// and vitest treats a function returned from a hook as a teardown callback,
// which would invoke the mock (recording a phantom no-arg call) after each test.
beforeEach(() => { mocks.installApp.mockResolvedValue({ name: 'zzq-app' }) })
afterEach(() => vi.clearAllMocks())

describe('SourcesPopover', () => {
  it('shows only the trigger while closed', () => {
    wrap(<SourcesPopover open={false} onOpenChange={() => {}} onError={() => {}} />)
    expect(screen.getByLabelText(i18nT('components.appstore.sourcesPopover.manage_app_sources'))).toBeInTheDocument()
    expect(screen.queryByTestId('zzq-registry-stub')).not.toBeInTheDocument()
  })

  it('renders the registry manager and the install form when open', () => {
    wrap(<SourcesPopover open onOpenChange={() => {}} onError={() => {}} />)
    expect(screen.getByTestId('zzq-registry-stub')).toBeInTheDocument()
    expect(screen.getByText(i18nT('components.appstore.sourcesPopover.install_from_path'))).toBeInTheDocument()
    expect(installBtn()).toBeDisabled()
  })

  it('does not install on Enter with a blank path', () => {
    wrap(<SourcesPopover open onOpenChange={() => {}} onError={() => {}} />)
    fireEvent.keyDown(pathInput(), { key: 'Enter' })
    expect(mocks.installApp).not.toHaveBeenCalled()
  })

  it('installs the trimmed path, records it, broadcasts, reports the name and closes', async () => {
    const onOpenChange = vi.fn()
    const onInstalled = vi.fn()
    const changed = vi.fn()
    window.addEventListener('mc:apps-changed', changed)

    wrap(<SourcesPopover open onOpenChange={onOpenChange} onError={() => {}} onInstalled={onInstalled} />)
    fireEvent.change(pathInput(), { target: { value: '  /zzq/path/app  ' } })
    fireEvent.click(installBtn())

    await waitFor(() => expect(mocks.installApp).toHaveBeenCalledWith('/zzq/path/app'))
    await waitFor(() => expect(onInstalled).toHaveBeenCalledWith('zzq-app'))
    expect(recordEventMock).toHaveBeenCalledWith('app_install', { app: 'zzq-app', source: 'local' })
    expect(changed).toHaveBeenCalled()
    expect(onOpenChange).toHaveBeenCalledWith(false)
    window.removeEventListener('mc:apps-changed', changed)
  })

  it('installs on Enter and falls back to the path when the backend returns no name', async () => {
    mocks.installApp.mockResolvedValue({})
    const onInstalled = vi.fn()
    wrap(<SourcesPopover open onOpenChange={() => {}} onError={() => {}} onInstalled={onInstalled} />)

    fireEvent.change(pathInput(), { target: { value: '/zzq/nameless' } })
    fireEvent.keyDown(pathInput(), { key: 'Enter' })

    await waitFor(() => expect(onInstalled).toHaveBeenCalledWith('/zzq/nameless'))
  })

  it('reports the failure message through onError and keeps the popover open', async () => {
    mocks.installApp.mockRejectedValue(new Error('zzq manifest missing'))
    const onError = vi.fn()
    const onOpenChange = vi.fn()
    wrap(<SourcesPopover open onOpenChange={onOpenChange} onError={onError} />)

    fireEvent.change(pathInput(), { target: { value: '/zzq/bad' } })
    fireEvent.click(installBtn())

    await waitFor(() => expect(onError).toHaveBeenCalledWith('zzq manifest missing'))
    expect(onOpenChange).not.toHaveBeenCalled()
    // The button is re-enabled: the `installing` flag is released in `finally`.
    await waitFor(() => expect(installBtn()).not.toBeDisabled())
  })

  it('falls back to the generic failure copy when the rejection carries no message', async () => {
    mocks.installApp.mockRejectedValue(undefined)
    const onError = vi.fn()
    wrap(<SourcesPopover open onOpenChange={() => {}} onError={onError} />)

    fireEvent.change(pathInput(), { target: { value: '/zzq/bad2' } })
    fireEvent.click(installBtn())

    await waitFor(() => expect(onError).toHaveBeenCalledWith(
      i18nT('components.appstore.sourcesPopover.install_failed'),
    ))
  })
})
