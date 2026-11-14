// The memory provider sheet and the App SDK call that opens it.
//
// Pinned here: an app's `requestMemoryProviderChange()` switches nothing by itself
// (no host -> 'unavailable'; a host -> the sheet, and only the owner's press calls
// the switch), the consent a remote provider needs gates the press, and the copy the
// sheet offers runs after the switch with the right direction, and a failed copy
// never reports a switch that happened as failed.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { api } from '../api/client'
import type { MemoryMove, MemoryProviderOverview, MemoryProviderRow } from '../api/client/memory'
import { requestMemoryProviderChange } from '../app-sdk/memoryProviderChange'
import { MemoryProviderSheetHost } from './MemoryProviderSheet'
import { i18nT } from '../i18n/t'

const CAPS: NonNullable<MemoryProviderRow['capabilities']> = {
  api_version: 1,
  provider: { name: 'fixture', version: '1.0.0' },
  egress: 'local',
  read_only: false,
  ops: ['context', 'search', 'get', 'list', 'put', 'delete'],
  kinds: ['rule', 'finding', 'preference', 'fact'],
  search: { lexical: true, semantic: false, filters: [] },
  writes: {
    verbatim: true,
    preserves_timestamps: true,
    keeps_origin: true,
    idempotent: true,
    dry_run_put: false,
    max_batch: 50,
    max_text: 8000,
  },
  history: { revisions: false, retired: true, changes: false },
  ingest: 'raw',
  isolation: { multi_store: true, principal_auth: false },
  documents: 'none',
  steering: 'none',
  max_page: 50,
}

function overview(egress: 'local' | 'remote' = 'local'): MemoryProviderOverview {
  return {
    mode: 'builtin',
    id: '',
    switched_at: 0,
    stop_learning_ack: false,
    builtin: { id: 'builtin', display_name: 'Crew memory' },
    providers: [
      {
        id: 'app:fixture/fixture',
        app: 'fixture',
        app_version: '1.0.0',
        display_name: 'Fixture memory',
        description: '',
        settings_page: '',
        enabled: true,
        capabilities: { ...CAPS, egress },
        replace_gaps: [],
        egress_consented: false,
      },
    ],
    shared_crew: false,
    policy_allows: true,
    active: { mode: 'builtin', id: 'builtin', unavailable: '' },
  }
}

function doneJob(outcomes: Record<string, number> = { inserted: 1 }): MemoryMove {
  return {
    id: 'j',
    from: 'builtin',
    to: 'app:fixture/fixture',
    store: 'default',
    dry_run: false,
    state: 'done',
    checkpoint: { cursor: null, listed: 1 },
    by_kind: { rule: 1 },
    outcomes,
    skipped_kinds: {},
    notes: [],
    verify: {},
    rolled_back: 0,
    error: '',
  }
}

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryProviderSheetHost />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('requestMemoryProviderChange', () => {
  it('answers unavailable when no host sheet is mounted', async () => {
    await expect(requestMemoryProviderChange({ mode: 'replace', id: 'app:fixture/fixture' })).resolves.toBe('unavailable')
  })

  it('refuses a target outside the contract without opening anything', async () => {
    mount()
    await expect(requestMemoryProviderChange({ mode: 'replace', id: 'https://elsewhere' })).resolves.toBe('unavailable')
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('switches only on the owner press, then copies Crew memory in', async () => {
    vi.spyOn(api, 'memoryProvider').mockResolvedValue(overview())
    const switched = vi.spyOn(api, 'switchMemoryProvider').mockResolvedValue({ ok: true, mode: 'replace' })
    const copied = vi.spyOn(api, 'startMemoryMove').mockResolvedValue({
      id: 'j', from: 'builtin', to: 'app:fixture/fixture', state: 'running', listed: 0, outcomes: {}, skipped_kinds: {}, error: '',
    })
    vi.spyOn(api, 'memoryMove').mockResolvedValue(doneJob())
    mount()
    let outcome: Promise<string> | undefined
    act(() => {
      outcome = requestMemoryProviderChange({ mode: 'replace', id: 'app:fixture/fixture' })
    })
    const confirm = await screen.findByRole('button', { name: i18nT('pages.overview.memoryProvider.confirm') })
    expect(switched).not.toHaveBeenCalled()
    await waitFor(() => expect(confirm).not.toBeDisabled())
    fireEvent.click(confirm)
    await expect(outcome).resolves.toBe('switched')
    expect(switched).toHaveBeenCalledWith({ mode: 'replace', id: 'app:fixture/fixture' })
    expect(copied).toHaveBeenCalledWith({ from: 'builtin', to: 'app:fixture/fixture' })
  })

  it('holds the press until the owner consents to a remote provider', async () => {
    vi.spyOn(api, 'memoryProvider').mockResolvedValue(overview('remote'))
    const switched = vi.spyOn(api, 'switchMemoryProvider').mockResolvedValue({ ok: true, mode: 'replace' })
    vi.spyOn(api, 'startMemoryMove').mockResolvedValue({
      id: 'j', from: 'builtin', to: 'app:fixture/fixture', state: 'running', listed: 0, outcomes: {}, skipped_kinds: {}, error: '',
    })
    vi.spyOn(api, 'memoryMove').mockResolvedValue(doneJob())
    mount()
    let outcome: Promise<string> | undefined
    act(() => {
      outcome = requestMemoryProviderChange({ mode: 'replace', id: 'app:fixture/fixture' })
    })
    const consent = await screen.findByLabelText(
      i18nT('pages.overview.memoryProvider.egress_consent', { name: 'Fixture memory' }),
    )
    const confirm = screen.getByRole('button', { name: i18nT('pages.overview.memoryProvider.confirm') })
    expect(confirm).toBeDisabled()
    fireEvent.click(consent)
    fireEvent.click(confirm)
    await expect(outcome).resolves.toBe('switched')
    expect(switched).toHaveBeenCalledWith({ mode: 'replace', id: 'app:fixture/fixture', egress_consent: true })
  })

  it('answers switched and names the failed copy when the copy after a switch fails', async () => {
    vi.spyOn(api, 'memoryProvider').mockResolvedValue(overview())
    vi.spyOn(api, 'switchMemoryProvider').mockResolvedValue({ ok: true, mode: 'replace' })
    vi.spyOn(api, 'startMemoryMove').mockRejectedValue(new Error('provider unavailable'))
    mount()
    let outcome: Promise<string> | undefined
    act(() => {
      outcome = requestMemoryProviderChange({ mode: 'replace', id: 'app:fixture/fixture' })
    })
    const confirm = await screen.findByRole('button', { name: i18nT('pages.overview.memoryProvider.confirm') })
    await waitFor(() => expect(confirm).not.toBeDisabled())
    fireEvent.click(confirm)
    expect(await screen.findByText(i18nT('pages.overview.memoryProvider.copy_failed'))).toBeTruthy()
    expect(screen.queryByText(i18nT('pages.overview.memoryProvider.error_switch_failed'))).toBeNull()
    fireEvent.click(screen.getAllByRole('button', { name: i18nT('pages.overview.memoryProvider.close') }).at(-1)!)
    await expect(outcome).resolves.toBe('switched')
  })

  it('follows the copy job and names a copy that left items behind', async () => {
    vi.spyOn(api, 'memoryProvider').mockResolvedValue(overview())
    vi.spyOn(api, 'switchMemoryProvider').mockResolvedValue({ ok: true, mode: 'replace' })
    vi.spyOn(api, 'startMemoryMove').mockResolvedValue({ ...doneJob(), state: 'running' })
    vi.spyOn(api, 'memoryMove').mockResolvedValue(doneJob({ inserted: 1, 'refused:at_capacity': 2 }))
    mount()
    let outcome: Promise<string> | undefined
    act(() => {
      outcome = requestMemoryProviderChange({ mode: 'replace', id: 'app:fixture/fixture' })
    })
    const confirm = await screen.findByRole('button', { name: i18nT('pages.overview.memoryProvider.confirm') })
    await waitFor(() => expect(confirm).not.toBeDisabled())
    fireEvent.click(confirm)
    expect(await screen.findByText(i18nT('pages.overview.memoryProvider.copy_partial'))).toBeTruthy()
    fireEvent.click(screen.getAllByRole('button', { name: i18nT('pages.overview.memoryProvider.close') }).at(-1)!)
    await expect(outcome).resolves.toBe('switched')
  })

  it('holds the press and names the failure when the overview cannot load', async () => {
    vi.spyOn(api, 'memoryProvider').mockRejectedValue(new Error('offline'))
    const switched = vi.spyOn(api, 'switchMemoryProvider')
    mount()
    act(() => {
      void requestMemoryProviderChange({ mode: 'replace', id: 'app:fixture/fixture' })
    })
    expect(await screen.findByText(i18nT('pages.overview.memoryProvider.load_failed'), {}, { timeout: 5000 })).toBeTruthy()
    expect(screen.getByRole('button', { name: i18nT('pages.overview.memoryProvider.confirm') })).toBeDisabled()
    expect(switched).not.toHaveBeenCalled()
  })

  it('answers cancelled when the owner closes the sheet', async () => {
    vi.spyOn(api, 'memoryProvider').mockResolvedValue(overview())
    const switched = vi.spyOn(api, 'switchMemoryProvider')
    mount()
    let outcome: Promise<string> | undefined
    act(() => {
      outcome = requestMemoryProviderChange({ mode: 'augment', id: 'app:fixture/fixture' })
    })
    fireEvent.click(await screen.findByRole('button', { name: i18nT('pages.overview.memoryProvider.cancel') }))
    await expect(outcome).resolves.toBe('cancelled')
    expect(switched).not.toHaveBeenCalled()
  })
})
