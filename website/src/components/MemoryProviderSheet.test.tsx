// The memory provider sheet and the App SDK call that opens it.
//
// Pinned here: an app's `requestMemoryProviderChange()` switches nothing by itself
// (no host -> 'unavailable'; a host -> the sheet, and only the owner's press calls
// the switch), the consent a remote provider needs gates the press, and the copy the
// sheet offers into a provider runs to its end before the switch, so a copy that
// fails or a sheet closed mid-copy switches nothing. A copy back into Crew memory
// runs after the switch, and a failed one never reports that switch as failed.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { api } from '../api/client'
import type { MemoryMove, MemoryProviderOverview, MemoryProviderRow } from '../api/client/memory'
import { requestMemoryProviderChange } from '../app-sdk/memoryProviderChange'
import { MemoryProviderSheetHost } from './MemoryProviderSheet'
import { i18nT } from '../i18n/t'
import { __resetNavSeamForTests } from '../utils/errorReport'

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
  __resetNavSeamForTests()
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
    // Copy start -> job query poll -> switch -> close: a bounded wait past the 1s default.
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull(), { timeout: 5000 })
    await expect(outcome).resolves.toBe('switched')
    expect(switched).toHaveBeenCalledWith({ mode: 'replace', id: 'app:fixture/fixture' })
    expect(copied).toHaveBeenCalledWith({ from: 'builtin', to: 'app:fixture/fixture' })
  })

  it('holds the press until the owner consents to a remote provider', async () => {
    vi.spyOn(api, 'memoryProvider').mockResolvedValue(overview('remote'))
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
    const consent = await screen.findByLabelText(
      i18nT('pages.overview.memoryProvider.egress_consent', { name: 'Fixture memory' }),
    )
    const confirm = screen.getByRole('button', { name: i18nT('pages.overview.memoryProvider.confirm') })
    expect(confirm).toBeDisabled()
    fireEvent.click(consent)
    fireEvent.click(confirm)
    // Copy start -> job query poll -> switch -> close: a bounded wait past the 1s default.
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull(), { timeout: 5000 })
    await expect(outcome).resolves.toBe('switched')
    expect(switched).toHaveBeenCalledWith({ mode: 'replace', id: 'app:fixture/fixture', egress_consent: true })
    // The copy runs before the switch, so it carries the consent the move needs.
    expect(copied).toHaveBeenCalledWith({ from: 'builtin', to: 'app:fixture/fixture', egress_consent: true })
  })

  it('keeps naming the provider it switched away from once the switch lands', async () => {
    const replaced = { ...overview(), mode: 'replace' as const, id: 'app:fixture/fixture' }
    const view = vi.spyOn(api, 'memoryProvider').mockResolvedValue(replaced)
    vi.spyOn(api, 'switchMemoryProvider').mockImplementation(async () => {
      view.mockResolvedValue(overview())
      return { ok: true, mode: 'builtin' }
    })
    vi.spyOn(api, 'startMemoryMove').mockRejectedValue(new Error('provider unavailable'))
    mount()
    act(() => {
      void requestMemoryProviderChange({ mode: 'builtin' })
    })
    const lead = i18nT('pages.overview.memoryProvider.sheet_body_builtin', { name: 'Fixture memory' })
    expect(await screen.findByText(lead)).toBeTruthy()
    const confirm = screen.getByRole('button', { name: i18nT('pages.overview.memoryProvider.confirm') })
    await waitFor(() => expect(confirm).not.toBeDisabled())
    fireEvent.click(confirm)
    await screen.findByText(i18nT('pages.overview.memoryProvider.copy_failed'))
    await waitFor(() => expect(view.mock.calls.length).toBeGreaterThan(1))
    expect(screen.getByText(lead)).toBeTruthy()
  })

  it('copies nothing when the owner re-confirms the provider already in use', async () => {
    const replaced = { ...overview(), mode: 'replace' as const, id: 'app:fixture/fixture' }
    vi.spyOn(api, 'memoryProvider').mockResolvedValue(replaced)
    const switched = vi.spyOn(api, 'switchMemoryProvider').mockResolvedValue({ ok: true, mode: 'replace' })
    const copied = vi.spyOn(api, 'startMemoryMove')
    mount()
    act(() => {
      void requestMemoryProviderChange({ mode: 'replace', id: 'app:fixture/fixture' })
    })
    const confirm = await screen.findByRole('button', { name: i18nT('pages.overview.memoryProvider.confirm') })
    await waitFor(() => expect(confirm).not.toBeDisabled())
    expect(screen.queryByRole('checkbox')).toBeNull()
    fireEvent.click(confirm)
    await waitFor(() => expect(switched).toHaveBeenCalled())
    expect(copied).not.toHaveBeenCalled()
    expect(screen.queryByText(i18nT('pages.overview.memoryProvider.copy_failed'))).toBeNull()
  })

  it('answers each row from the provider handshake', async () => {
    vi.spyOn(api, 'memoryProvider').mockResolvedValue(overview('remote'))
    mount()
    act(() => {
      void requestMemoryProviderChange({ mode: 'replace', id: 'app:fixture/fixture' })
    })
    const rows = await screen.findByTestId('memory-provider-rows')
    await waitFor(() =>
      expect(rows.textContent).toContain(i18nT('pages.overview.memoryProvider.where_remote')),
    )
    expect(rows.textContent).toContain(i18nT('pages.overview.memoryProvider.access_all'))
    expect(rows.textContent).toContain(i18nT('pages.overview.memoryProvider.existing_copied', { name: 'Crew memory' }))
    // The tick sits outside the read-only rows and names both ends.
    expect(rows.querySelector('input[type="checkbox"]')).toBeNull()
    fireEvent.click(screen.getByLabelText('Copy Crew memory into Fixture memory first'))
    expect(rows.textContent).toContain(i18nT('pages.overview.memoryProvider.existing_not_copied_from', { name: 'Crew memory' }))
  })

  it('switches nothing and answers cancelled when the copy that runs first fails', async () => {
    vi.spyOn(api, 'memoryProvider').mockResolvedValue(overview())
    const switched = vi.spyOn(api, 'switchMemoryProvider').mockResolvedValue({ ok: true, mode: 'replace' })
    vi.spyOn(api, 'startMemoryMove').mockRejectedValue(new Error('provider unavailable'))
    mount()
    let outcome: Promise<string> | undefined
    act(() => {
      outcome = requestMemoryProviderChange({ mode: 'replace', id: 'app:fixture/fixture' })
    })
    const confirm = await screen.findByRole('button', { name: i18nT('pages.overview.memoryProvider.confirm') })
    await waitFor(() => expect(confirm).not.toBeDisabled())
    fireEvent.click(confirm)
    expect(await screen.findByText(i18nT('pages.overview.memoryProvider.copy_failed_first'))).toBeTruthy()
    fireEvent.click(screen.getAllByRole('button', { name: i18nT('pages.overview.memoryProvider.close') }).at(-1)!)
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    await expect(outcome).resolves.toBe('cancelled')
    expect(switched).not.toHaveBeenCalled()
  })

  it('copies first, switches once the copy is done, and switches nothing when closed mid-copy', async () => {
    vi.spyOn(api, 'memoryProvider').mockResolvedValue(overview())
    const switched = vi.spyOn(api, 'switchMemoryProvider').mockResolvedValue({ ok: true, mode: 'replace' })
    vi.spyOn(api, 'startMemoryMove').mockResolvedValue({ ...doneJob(), state: 'running' })
    const job = vi.spyOn(api, 'memoryMove').mockResolvedValue({ ...doneJob(), state: 'running' })
    mount()
    let outcome: Promise<string> | undefined
    act(() => {
      outcome = requestMemoryProviderChange({ mode: 'replace', id: 'app:fixture/fixture' })
    })
    let confirm = await screen.findByRole('button', { name: i18nT('pages.overview.memoryProvider.confirm') })
    await waitFor(() => expect(confirm).not.toBeDisabled())
    fireEvent.click(confirm)
    expect(await screen.findByText(i18nT('pages.overview.memoryProvider.copy_running_first'))).toBeTruthy()
    expect(switched).not.toHaveBeenCalled()
    fireEvent.click(screen.getAllByRole('button', { name: i18nT('pages.overview.memoryProvider.close') }).at(-1)!)
    await expect(outcome).resolves.toBe('cancelled')
    expect(switched).not.toHaveBeenCalled()

    job.mockResolvedValue(doneJob())
    act(() => {
      outcome = requestMemoryProviderChange({ mode: 'replace', id: 'app:fixture/fixture' })
    })
    confirm = await screen.findByRole('button', { name: i18nT('pages.overview.memoryProvider.confirm') })
    await waitFor(() => expect(confirm).not.toBeDisabled())
    fireEvent.click(confirm)
    await expect(outcome).resolves.toBe('switched')
    expect(switched).toHaveBeenCalledWith({ mode: 'replace', id: 'app:fixture/fixture' })
  })

  // A conflict is an item edited on both sides since an earlier copy: left as it is.
  it.each(['refused:at_capacity', 'conflict:changed_since_copy'])('follows the copy job and names a copy that left items behind (%s)', async left => {
    vi.spyOn(api, 'memoryProvider').mockResolvedValue(overview())
    const switched = vi.spyOn(api, 'switchMemoryProvider').mockResolvedValue({ ok: true, mode: 'replace' })
    vi.spyOn(api, 'startMemoryMove').mockResolvedValue({ ...doneJob(), state: 'running' })
    vi.spyOn(api, 'memoryMove').mockResolvedValue(doneJob({ inserted: 1, [left]: 2 }))
    mount()
    let outcome: Promise<string> | undefined
    act(() => {
      outcome = requestMemoryProviderChange({ mode: 'replace', id: 'app:fixture/fixture' })
    })
    const confirm = await screen.findByRole('button', { name: i18nT('pages.overview.memoryProvider.confirm') })
    await waitFor(() => expect(confirm).not.toBeDisabled())
    fireEvent.click(confirm)
    expect(await screen.findByText(i18nT('pages.overview.memoryProvider.copy_partial_first'))).toBeTruthy()
    fireEvent.click(screen.getAllByRole('button', { name: i18nT('pages.overview.memoryProvider.close') }).at(-1)!)
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    await expect(outcome).resolves.toBe('cancelled')
    expect(switched).not.toHaveBeenCalled()
  })

  it('switches nothing when the copy skipped a kind the provider does not hold', async () => {
    vi.spyOn(api, 'memoryProvider').mockResolvedValue(overview())
    const switched = vi.spyOn(api, 'switchMemoryProvider').mockResolvedValue({ ok: true, mode: 'replace' })
    vi.spyOn(api, 'startMemoryMove').mockResolvedValue({ ...doneJob(), state: 'running' })
    vi.spyOn(api, 'memoryMove').mockResolvedValue({ ...doneJob(), skipped_kinds: { episode: 3, document: 2 } })
    mount()
    let outcome: Promise<string> | undefined
    act(() => {
      outcome = requestMemoryProviderChange({ mode: 'replace', id: 'app:fixture/fixture' })
    })
    const confirm = await screen.findByRole('button', { name: i18nT('pages.overview.memoryProvider.confirm') })
    await waitFor(() => expect(confirm).not.toBeDisabled())
    fireEvent.click(confirm)
    expect(await screen.findByText(i18nT('pages.overview.memoryProvider.copy_partial_first'))).toBeTruthy()
    fireEvent.click(screen.getAllByRole('button', { name: i18nT('pages.overview.memoryProvider.close') }).at(-1)!)
    await expect(outcome).resolves.toBe('cancelled')
    expect(switched).not.toHaveBeenCalled()
  })

  it('holds Close while the switch after a finished copy is still on its way', async () => {
    vi.spyOn(api, 'memoryProvider').mockResolvedValue(overview())
    let land: (v: { ok: boolean; mode: string }) => void = () => {}
    const switched = vi.spyOn(api, 'switchMemoryProvider').mockImplementation(() => new Promise(resolve => { land = resolve }))
    vi.spyOn(api, 'startMemoryMove').mockResolvedValue({ ...doneJob(), state: 'running' })
    vi.spyOn(api, 'memoryMove').mockResolvedValue(doneJob())
    mount()
    let outcome: Promise<string> | undefined
    act(() => {
      outcome = requestMemoryProviderChange({ mode: 'replace', id: 'app:fixture/fixture' })
    })
    const confirm = await screen.findByRole('button', { name: i18nT('pages.overview.memoryProvider.confirm') })
    await waitFor(() => expect(confirm).not.toBeDisabled())
    fireEvent.click(confirm)
    await waitFor(() => expect(switched).toHaveBeenCalled())
    for (const btn of screen.getAllByRole('button', { name: i18nT('pages.overview.memoryProvider.close') })) {
      expect(btn).toBeDisabled()
    }
    act(() => land({ ok: true, mode: 'replace' }))
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

describe('MemoryProviderSheet copy again', () => {
  it('offers Copy again after a copy that failed and starts the same move', async () => {
    vi.spyOn(api, 'memoryProvider').mockResolvedValue(overview())
    vi.spyOn(api, 'switchMemoryProvider').mockResolvedValue({ ok: true, mode: 'replace' })
    const start = vi
      .spyOn(api, 'startMemoryMove')
      .mockRejectedValueOnce(new Error('provider unavailable'))
      .mockResolvedValueOnce(doneJob())
    vi.spyOn(api, 'memoryMove').mockResolvedValue(doneJob())
    mount()
    act(() => {
      void requestMemoryProviderChange({ mode: 'replace', id: 'app:fixture/fixture' })
    })
    const confirm = await screen.findByRole('button', { name: i18nT('pages.overview.memoryProvider.confirm') })
    await waitFor(() => expect(confirm).not.toBeDisabled())
    fireEvent.click(confirm)
    const again = await screen.findByRole('button', { name: i18nT('pages.overview.memoryProvider.copy_again') })
    fireEvent.click(again)
    await waitFor(() => expect(start).toHaveBeenCalledTimes(2))
    expect(start).toHaveBeenLastCalledWith({ from: 'builtin', to: 'app:fixture/fixture' })
  })
})

describe('memory provider approval semantics', () => {
  it('adds a read-only source without promising a switch or starting a copy', async () => {
    vi.spyOn(api, 'memoryProvider').mockResolvedValue(overview())
    const switched = vi.spyOn(api, 'switchMemoryProvider').mockResolvedValue({ ok: true, mode: 'augment' })
    const copied = vi.spyOn(api, 'startMemoryMove')
    mount()
    let outcome: Promise<string> | undefined
    act(() => { outcome = requestMemoryProviderChange({ mode: 'augment', id: 'app:fixture/fixture' }) })
    await screen.findByRole('dialog', { name: 'Add Fixture memory as a source?' })
    const add = screen.getByRole('button', { name: 'Add source' })
    await waitFor(() => expect(add).not.toBeDisabled())
    expect(screen.queryByRole('button', { name: 'Switch' })).toBeNull()
    fireEvent.click(add)
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    await expect(outcome).resolves.toBe('switched')
    expect(switched).toHaveBeenCalledWith({ mode: 'augment', id: 'app:fixture/fixture' })
    expect(copied).not.toHaveBeenCalled()
  })

  it('closes the add-source sheet with a note that names adding a source', async () => {
    vi.spyOn(api, 'memoryProvider').mockResolvedValue(overview())
    mount()
    act(() => { void requestMemoryProviderChange({ mode: 'augment', id: 'app:fixture/fixture' }) })
    await screen.findByRole('dialog', { name: 'Add Fixture memory as a source?' })
    expect(screen.getByText(i18nT('pages.overview.memoryProvider.host_note_add'))).toBeTruthy()
    expect(screen.queryByText(i18nT('pages.overview.memoryProvider.host_note'))).toBeNull()
  })

  it('describes the leaving source on every row when the owner stops reading it', async () => {
    vi.spyOn(api, 'memoryProvider').mockResolvedValue({ ...overview('remote'), mode: 'augment', id: 'app:fixture/fixture' })
    mount()
    act(() => { void requestMemoryProviderChange({ mode: 'builtin' }) })
    await screen.findByRole('button', { name: i18nT('pages.overview.memoryProvider.confirm_stop') })
    const rows = screen.getByTestId('memory-provider-rows')
    await waitFor(() => expect(rows.textContent).toContain('Fixture memory'))
    expect(rows.textContent).toContain(i18nT('pages.overview.memoryProvider.access_read_only'))
    expect(rows.textContent).not.toContain(i18nT('pages.overview.memoryProvider.access_all'))
    expect(rows.textContent).toContain(i18nT('pages.overview.memoryProvider.where_remote'))
  })

  // A copy back into Crew memory runs right after the switch, so no line says "first".
  it('names the source in both switch-back copy choices and omits switching back again', async () => {
    vi.spyOn(api, 'memoryProvider').mockResolvedValue({ ...overview(), mode: 'replace', id: 'app:fixture/fixture' })
    mount()
    act(() => { void requestMemoryProviderChange({ mode: 'builtin' }) })
    await screen.findByText('Copied back from Fixture memory right after the switch')
    expect(screen.queryByText('Switching back')).toBeNull()
    expect(screen.queryByText(/first/)).toBeNull()
    fireEvent.click(screen.getByLabelText('Copy Fixture memory back into Crew memory right after the switch'))
    expect(screen.getByText('Not copied from Fixture memory')).toBeTruthy()
  })

  it('copies from the active replacement and keeps that source for retry', async () => {
    const original = overview()
    original.providers.push({ ...original.providers[0], id: 'app:second/memory', display_name: 'Second memory' })
    vi.spyOn(api, 'memoryProvider').mockResolvedValue({ ...original, mode: 'replace', id: 'app:fixture/fixture' })
    const switched = vi.spyOn(api, 'switchMemoryProvider')
    const start = vi.spyOn(api, 'startMemoryMove').mockRejectedValue(new Error('offline'))
    mount()
    let outcome: Promise<string> | undefined
    act(() => { outcome = requestMemoryProviderChange({ mode: 'replace', id: 'app:second/memory' }) })
    await screen.findByText('Copied from Fixture memory first')
    const confirm = screen.getByRole('button', { name: 'Switch' })
    await waitFor(() => expect(confirm).not.toBeDisabled())
    fireEvent.click(confirm)
    await screen.findByText(i18nT('pages.overview.memoryProvider.copy_failed_first'))
    expect(start).toHaveBeenCalledWith({ from: 'app:fixture/fixture', to: 'app:second/memory' })
    expect(screen.getByText('Copied from Fixture memory first')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Copy again' }))
    await waitFor(() => expect(start).toHaveBeenCalledTimes(2))
    expect(start).toHaveBeenLastCalledWith({ from: 'app:fixture/fixture', to: 'app:second/memory' })
    fireEvent.click(screen.getAllByRole('button', { name: i18nT('pages.overview.memoryProvider.close') }).at(-1)!)
    await expect(outcome).resolves.toBe('cancelled')
    expect(switched).not.toHaveBeenCalled()
  })
})

describe('MemoryProviderPicker verdict and links', () => {
  async function mountPicker(data: MemoryProviderOverview) {
    const { default: Picker } = await import('../pages/settings/MemoryProviderPicker')
    vi.spyOn(api, 'memoryProvider').mockResolvedValue(data)
    vi.spyOn(api, 'memoryMoves').mockResolvedValue({ migrations: [] })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={client}><Picker /></QueryClientProvider>)
    await screen.findByTestId('memory-provider-current')
  }

  it.each([
    { health: undefined, unavailable: '', ok: true },
    { health: { ok: true, detail: '' }, unavailable: '', ok: true },
    { health: { ok: false, detail: 'offline' }, unavailable: '', ok: false },
    { health: { ok: true, detail: '' }, unavailable: 'blocked', ok: false },
  ])('honors health and availability: %j', async ({ health, unavailable, ok }) => {
    const data = overview()
    data.active = { ...data.active, health, unavailable }
    await mountPicker(data)
    expect(screen.getByTitle(i18nT(ok ? 'pages.overview.memoryProvider.health_ok' : 'pages.overview.memoryProvider.health_down'))).toBeTruthy()
  })

  it('puts enabled copy links below the provider name on narrow screens', async () => {
    await mountPicker(overview())
    const link = screen.getByRole('button', { name: 'Copy Crew memory here' })
    expect(link).not.toHaveClass('text-muted')
    expect(link).toHaveClass('text-accent')
    expect(link.parentElement).toHaveClass('col-start-2', 'sm:col-start-3', 'sm:row-start-1')
    expect(link.closest('li')).toHaveClass('grid-cols-[28px_minmax(0,1fr)]')
  })

  it('explains read-only providers without listing duplicate save capabilities', async () => {
    const data = overview()
    data.providers[0].capabilities = { ...CAPS, read_only: true, ops: ['search'] }
    data.providers[0].replace_gaps = ['read_only', 'op:put', 'writes.verbatim']
    await mountPicker(data)
    expect(screen.getByText("Read only: it can't save memories.")).toBeTruthy()
    expect(screen.queryByText(/Cannot replace Crew memory because/)).toBeNull()
    expect(screen.getByRole('button', { name: 'Add as source' })).toBeTruthy()
  })

  it('names every gap that keeps a provider from replacing Crew memory', async () => {
    const data = overview()
    data.providers[0].replace_gaps = [
      'op:get', 'op:list', 'op:put', 'op:delete', 'op:search', 'op:context', 'kind:finding',
      'read_only', 'writes.verbatim', 'writes.idempotent', 'op:context_or_search', 'other.gap',
    ]
    await mountPicker(data)
    const text = screen.getByTestId('memory-provider-picker').textContent ?? ''
    for (const key of ['op_get', 'op_list', 'op_put', 'op_delete', 'op_search', 'read_only', 'verbatim', 'idempotent', 'context_or_search']) {
      expect(text).toContain(i18nT(`pages.overview.memoryProvider.gap.${key}`))
    }
    expect(text).toContain(i18nT('pages.overview.memoryProvider.gap.op', { op: 'context' }))
    expect(text).toContain('other.gap')
  })

  it('shows a replacement in use, switches back through the sheet and offers Crew memory', async () => {
    await mountPicker({ ...overview(), mode: 'replace', id: 'app:fixture/fixture' })
    const current = screen.getByTestId('memory-provider-current')
    expect(current.textContent).toContain('Fixture memory')
    expect(current.textContent).toContain(i18nT('pages.overview.memoryProvider.mode_replace'))
    expect(current.textContent).toContain(i18nT('pages.overview.memoryProvider.backup_note'))
    fireEvent.click(screen.getByRole('button', { name: i18nT('pages.overview.memoryProvider.switch_back') }))
    expect(await screen.findByRole('dialog')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: i18nT('pages.overview.memoryProvider.cancel') }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    fireEvent.click(screen.getByRole('button', { name: i18nT('pages.overview.memoryProvider.switch_link') }))
    expect(await screen.findByRole('dialog')).toBeTruthy()
  })

  it('shows a source being read and offers to stop it', async () => {
    await mountPicker({ ...overview(), mode: 'augment', id: 'app:fixture/fixture' })
    expect(screen.getByTestId('memory-provider-current').textContent).toContain(
      i18nT('pages.overview.memoryProvider.picker_mode_augment', { name: 'Fixture memory' }),
    )
    expect(screen.getByRole('button', { name: i18nT('pages.overview.memoryProvider.confirm_stop') })).toBeTruthy()
  })

  it('opens the switch sheet and the copy sheet from a provider row', async () => {
    vi.spyOn(api, 'startMemoryMove').mockResolvedValue({ ...doneJob(), dry_run: true, state: 'planned' })
    vi.spyOn(api, 'memoryMove').mockResolvedValue({ ...doneJob(), dry_run: true, state: 'planned' })
    await mountPicker(overview())
    fireEvent.click(screen.getByRole('button', { name: i18nT('pages.overview.memoryProvider.switch_link') }))
    expect(await screen.findByTestId('memory-provider-sheet')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: i18nT('pages.overview.memoryProvider.cancel') }))
    await waitFor(() => expect(screen.queryByTestId('memory-provider-sheet')).toBeNull())
    fireEvent.click(screen.getByRole('button', { name: 'Copy Crew memory here' }))
    expect(await screen.findByTestId('memory-move-sheet')).toBeTruthy()
    fireEvent.click(screen.getAllByRole('button', { name: i18nT('pages.overview.memoryProvider.close') }).at(-1)!)
    await waitFor(() => expect(screen.queryByTestId('memory-move-sheet')).toBeNull())
  })

  it('adds a read-only source from its row', async () => {
    const data = overview()
    data.providers[0].capabilities = { ...CAPS, read_only: true, ops: ['search'] }
    data.providers[0].replace_gaps = ['read_only']
    await mountPicker(data)
    fireEvent.click(screen.getByRole('button', { name: 'Add as source' }))
    expect(await screen.findByRole('dialog', { name: 'Add Fixture memory as a source?' })).toBeTruthy()
  })

  it('lists each unfinished copy by its state and names the notes and empty states', async () => {
    const { default: Picker } = await import('../pages/settings/MemoryProviderPicker')
    vi.spyOn(api, 'memoryProvider').mockResolvedValue({ ...overview(), providers: [], shared_crew: true, policy_allows: false })
    vi.spyOn(api, 'memoryMoves').mockResolvedValue({
      migrations: [
        { ...doneJob(), id: 'a', state: 'paused' },
        { ...doneJob(), id: 'b', state: 'failed' },
        { ...doneJob(), id: 'c', state: 'rollback_failed' },
        { ...doneJob(), id: 'd', state: 'paused', dry_run: true },
      ],
    })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={client}><Picker /></QueryClientProvider>)
    const list = await screen.findByTestId('memory-move-unfinished')
    for (const state of ['move_paused', 'move_failed', 'move_rollback_failed']) {
      expect(list.textContent).toContain(i18nT(`pages.overview.memoryProvider.${state}`))
    }
    expect(within(list).getAllByRole('button')).toHaveLength(3)
    const text = screen.getByTestId('memory-provider-picker').textContent ?? ''
    expect(text).toContain(i18nT('pages.overview.memoryProvider.none_installed'))
    expect(text).toContain(i18nT('pages.overview.memoryProvider.shared_crew_note'))
    expect(text).toContain(i18nT('pages.overview.memoryProvider.policy_note'))
  })

  it('says so when the overview or the copy list cannot be read', async () => {
    const { default: Picker } = await import('../pages/settings/MemoryProviderPicker')
    vi.spyOn(api, 'memoryProvider').mockRejectedValue(new Error('offline'))
    vi.spyOn(api, 'memoryMoves').mockRejectedValue(new Error('offline'))
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={client}><Picker /></QueryClientProvider>)
    expect(await screen.findByText(i18nT('pages.overview.memoryProvider.load_failed'))).toBeTruthy()
  })
})
