// The memory provider Feature preview on the host side.
//
// Pinned here: with the gateway answering off, no sheet host mounts, every memory verb
// settles 'not_enabled' without opening anything, and the memoryAdmin grant line says
// it is not enabled on this Crew; with it on, the hosts mount and the verbs reach them.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api } from '../api/client'
import { requestMemoryProviderChange } from '../app-sdk/memoryProviderChange'
import { requestMemoryMove } from '../app-sdk/memoryMove'
import { requestMemoryBench } from '../app-sdk/memoryBench'
import { memoryProviderPreviewOff, setMemoryProviderPreviewOff } from '../app-sdk/memoryProviderPreview'
import { apiGrantLabel, MEMORY_ADMIN_API_PREFIX } from './appstore/trustGrants'
import MemoryProviderSheetHosts from './MemoryProviderSheetHosts'
import { memoryProviderPreviewKey } from '../pages/settings/memoryProviderPreviewSetting'
import { i18nT } from '../i18n/t'

vi.mock('../api/client', async importOriginal => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, api: { ...actual.api, memoryProviderPreview: vi.fn() } }
})
vi.mock('./MemoryProviderSheet', () => ({ MemoryProviderSheetHost: () => <div data-testid="provider-host" /> }))
vi.mock('./MemoryMoveSheet', () => ({ MemoryMoveSheetHost: () => <div data-testid="move-host" /> }))
vi.mock('./MemoryBenchSheet', () => ({ MemoryBenchSheetHost: () => <div data-testid="bench-host" /> }))

const read = vi.mocked(api.memoryProviderPreview)
const setting = (active: boolean) => ({ enabled: active, active, env_override: false, restart_required: false })

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={qc}><MemoryProviderSheetHosts /></QueryClientProvider>)
}

beforeEach(() => {
  read.mockReset()
})

afterEach(() => {
  setMemoryProviderPreviewOff(false)
})

describe('memory provider preview off', () => {
  it('mounts no sheet, answers not_enabled and labels the grant as not enabled', async () => {
    read.mockResolvedValue(setting(false))
    mount()
    await waitFor(() => expect(memoryProviderPreviewOff()).toBe(true))
    expect(screen.queryByTestId('provider-host')).toBeNull()
    expect(screen.queryByTestId('move-host')).toBeNull()
    expect(screen.queryByTestId('bench-host')).toBeNull()
    await expect(requestMemoryProviderChange({ mode: 'builtin' })).resolves.toBe('not_enabled')
    await expect(requestMemoryMove({ from: 'builtin', to: 'app:a/b' })).resolves.toBe('not_enabled')
    await expect(requestMemoryBench({ suite: 'own:e_1', providers: ['builtin'] })).resolves.toEqual({ outcome: 'not_enabled' })
    expect(apiGrantLabel(MEMORY_ADMIN_API_PREFIX)).toBe(i18nT('pages.overview.memoryProvider.admin_grant_not_enabled'))
  })

  it('a refusal reads as off', async () => {
    read.mockRejectedValue(Object.assign(new Error('owner only'), { status: 403 }))
    mount()
    await waitFor(() => expect(memoryProviderPreviewOff()).toBe(true))
    expect(screen.queryByTestId('provider-host')).toBeNull()
  })
})

describe('memory provider preview on', () => {
  it('mounts the three sheet hosts and leaves the verbs to them', async () => {
    read.mockResolvedValue(setting(true))
    mount()
    // Preview read -> lazy sheet chunk load -> host render: a bounded wait past the 1s default.
    expect(await screen.findByTestId('provider-host', {}, { timeout: 5000 })).toBeInTheDocument()
    expect(screen.getByTestId('move-host')).toBeInTheDocument()
    expect(screen.getByTestId('bench-host')).toBeInTheDocument()
    expect(memoryProviderPreviewOff()).toBe(false)
    expect(apiGrantLabel(MEMORY_ADMIN_API_PREFIX)).toBe(i18nT('pages.overview.memoryProvider.admin_grant'))
  })
})

describe('memory provider preview read failed', () => {
  it('is never taken for off: the verbs answer unavailable, not not_enabled', async () => {
    read.mockRejectedValue(new TypeError('Failed to fetch'))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={qc}><MemoryProviderSheetHosts /></QueryClientProvider>)
    await waitFor(() => expect(qc.getQueryState(memoryProviderPreviewKey)?.status).toBe('error'))
    expect(memoryProviderPreviewOff()).toBe(false)
    expect(screen.queryByTestId('provider-host')).toBeNull()
    await expect(requestMemoryProviderChange({ mode: 'builtin' })).resolves.toBe('unavailable')
    expect(apiGrantLabel(MEMORY_ADMIN_API_PREFIX)).toBe(i18nT('pages.overview.memoryProvider.admin_grant'))
  })
})
