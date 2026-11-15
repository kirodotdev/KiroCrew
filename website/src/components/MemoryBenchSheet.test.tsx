// The benchmark sheet and the App SDK call that opens it.
//
// Pinned here: `requestMemoryBench()` starts nothing by itself (no host or a bad
// request -> 'unavailable'); the sheet plans first (a dry run), shows egress and
// cleanup per provider, needs the owner's tick before own memory leaves the
// machine, and starts the run only on the owner's press.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { api } from '../api/client'
import type { MemoryBenchPlan } from '../api/client/memory'
import { requestMemoryBench } from '../app-sdk/memoryBench'
import { MemoryBenchSheetHost } from './MemoryBenchSheet'
import { i18nT } from '../i18n/t'
import { installSoftNavigate, setGatewayConnected, __resetNavSeamForTests } from '../utils/errorReport'

function plan(egress: 'local' | 'remote'): MemoryBenchPlan {
  return {
    suite: { id: 'own', name: 'Your own memory', license: 'yours', license_note: '', restricted: false },
    units: 1,
    queries: 40,
    items: 120,
    llm_calls_estimate: 0,
    max_llm_calls: 2000,
    providers: [
      {
        id: 'app:cloud/mem',
        name: 'Cloud memory',
        egress,
        // The handshake version and the app version differ; consent names the app's.
        version: '0.9.2',
        app_version: '1.4.0',
        cleanup: 'reset',
        ingest_mode: 'items',
        ingest_why: '',
        writes: 120,
        turns: 0,
        na: { M3: 'measured without as_of' },
      },
    ],
  }
}

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryBenchSheetHost />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  __resetNavSeamForTests()
  vi.restoreAllMocks()
})

describe('requestMemoryBench', () => {
  it('answers unavailable with no host and for a malformed request', async () => {
    await expect(requestMemoryBench({ suite: 'own:e_1', providers: ['builtin'] })).resolves.toEqual({ outcome: 'unavailable' })
    mount()
    await expect(requestMemoryBench({ suite: '', providers: ['builtin'] })).resolves.toEqual({ outcome: 'unavailable' })
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('needs the owner tick before a copy of own memory leaves, then starts on the press', async () => {
    const start = vi
      .spyOn(api, 'startMemoryBench')
      .mockResolvedValueOnce({ run_id: 'r_1', state: 'planned', plan: plan('remote') })
      .mockResolvedValueOnce({ run_id: 'r_2', state: 'running', plan: plan('remote') })
    vi.spyOn(api, 'memoryBenchRun').mockResolvedValue({
      id: 'r_2', state: 'done', plan: plan('remote'), progress: {}, warnings: [], error: '',
    })
    mount()
    let outcome: Promise<unknown> | undefined
    act(() => {
      outcome = requestMemoryBench({ suite: 'own:e_1', providers: ['app:cloud/mem'], dry_run: false })
    })
    await screen.findByTestId('memory-bench-sheet')
    await waitFor(() => expect(start).toHaveBeenCalledWith({ suite: 'own:e_1', providers: ['app:cloud/mem'], dry_run: true }))
    await screen.findByText(i18nT('pages.overview.memoryBench.egress_remote'))
    const run = screen.getByRole('button', { name: i18nT('pages.overview.memoryBench.run') })
    expect(run).toBeDisabled()
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.click(run)
    await waitFor(() =>
      expect(start).toHaveBeenLastCalledWith({
        suite: 'own:e_1',
        providers: ['app:cloud/mem'],
        dry_run: false,
        egress_ack: { 'app:cloud/mem': { app_version: '1.4.0', items: 120 } },
      }),
    )
    // Run start -> run query poll -> report: a bounded wait past the 1s default.
    await screen.findByText(i18nT('pages.overview.memoryBench.done'), {}, { timeout: 5000 })
    const closes = screen.getAllByRole('button', { name: i18nT('pages.overview.memoryProvider.close') })
    fireEvent.click(closes[closes.length - 1])
    await expect(outcome).resolves.toEqual({ outcome: 'started', runId: 'r_2' })
  })

  it('shows a finished run\'s cleanup warnings', async () => {
    vi.spyOn(api, 'startMemoryBench')
      .mockResolvedValueOnce({ run_id: 'r_1', state: 'planned', plan: plan('local') })
      .mockResolvedValueOnce({ run_id: 'r_2', state: 'running', plan: plan('local') })
    vi.spyOn(api, 'memoryBenchRun').mockResolvedValue({
      id: 'r_2', state: 'done', plan: plan('local'), progress: {}, error: '',
      warnings: ['app:cloud/mem: bench_x did not read back empty after reset'],
    })
    mount()
    act(() => {
      void requestMemoryBench({ suite: 'own:e_1', providers: ['app:cloud/mem'] })
    })
    const run = await screen.findByRole('button', { name: i18nT('pages.overview.memoryBench.run') })
    await waitFor(() => expect(run).not.toBeDisabled())
    fireEvent.click(run)
    // Run start -> run query poll -> report: a bounded wait past the 1s default.
    await screen.findByText(i18nT('pages.overview.memoryBench.done'), {}, { timeout: 5000 })
    expect(await screen.findByText(/bench_x did not read back empty/)).toBeTruthy()
    expect(screen.getByRole('button', { name: i18nT('components.askAgent.ask_the_agent') })).toBeTruthy()
  })

  it('runs the plan the owner saw even if the app changes its arrays afterwards', async () => {
    const start = vi
      .spyOn(api, 'startMemoryBench')
      .mockResolvedValue({ run_id: 'r_1', state: 'planned', plan: plan('local') })
    mount()
    const providers = ['app:cloud/mem']
    const k = [5]
    act(() => {
      void requestMemoryBench({ suite: 'own:e_1', providers, k })
    })
    const run = await screen.findByRole('button', { name: i18nT('pages.overview.memoryBench.run') })
    await waitFor(() => expect(run).not.toBeDisabled())
    providers[0] = 'app:other/mem'
    k[0] = 50
    fireEvent.click(run)
    await waitFor(() =>
      expect(start).toHaveBeenLastCalledWith(
        expect.objectContaining({ providers: ['app:cloud/mem'], k: [5], dry_run: false }),
      ),
    )
  })

  it('asks for no tick when every provider stays local', async () => {
    vi.spyOn(api, 'startMemoryBench').mockResolvedValue({ run_id: 'r_1', state: 'planned', plan: plan('local') })
    mount()
    act(() => {
      void requestMemoryBench({ suite: 'own:e_1', providers: ['app:cloud/mem'] })
    })
    const run = await screen.findByRole('button', { name: i18nT('pages.overview.memoryBench.run') })
    await waitFor(() => expect(run).not.toBeDisabled())
    expect(screen.queryByRole('checkbox')).toBeNull()
  })

  it('says what the run scores, names the test set without its licence and hides a zero call estimate', async () => {
    vi.spyOn(api, 'startMemoryBench').mockResolvedValue({ run_id: 'r_1', state: 'planned', plan: plan('local') })
    mount()
    act(() => {
      void requestMemoryBench({ suite: 'own:e_1', providers: ['app:cloud/mem'] })
    })
    const rows = await screen.findByTestId('memory-bench-rows')
    expect(screen.getByText(i18nT('pages.overview.memoryBench.body', { n: '40' }))).toBeTruthy()
    expect(rows.textContent).toContain('Your own memory')
    expect(rows.textContent).not.toContain('yours')
    expect(rows.textContent).toContain(i18nT('pages.overview.memoryBench.row_questions'))
    expect(rows.textContent).not.toContain(i18nT('pages.overview.memoryBench.row_calls'))
  })
})

describe('MemoryBenchSheet error handoff', () => {
  it.each([false, true])('closes the sheet and settles its request after handoff (started: %s)', async started => {
    const start = vi.spyOn(api, 'startMemoryBench')
    if (started) {
      start.mockResolvedValueOnce({ run_id: 'plan', state: 'planned', plan: plan('local') })
        .mockResolvedValueOnce({ run_id: 'run', state: 'running', plan: plan('local') })
      vi.spyOn(api, 'memoryBenchRun').mockRejectedValue(new Error('offline'))
    } else start.mockRejectedValue(new Error('offline'))
    const navigate = vi.fn()
    installSoftNavigate(navigate)
    setGatewayConnected(true)
    mount()
    let outcome: Promise<unknown> | undefined
    act(() => { outcome = requestMemoryBench({ suite: 'own:e_1', providers: ['app:cloud/mem'] }) })
    if (started) {
      const run = await screen.findByRole('button', { name: i18nT('pages.overview.memoryBench.run') })
      await waitFor(() => expect(run).not.toBeDisabled())
      fireEvent.click(run)
    }
    await screen.findByText(i18nT('pages.overview.memoryBench.failed'))
    fireEvent.click(screen.getByRole('button', { name: i18nT('components.askAgent.ask_the_agent') }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(navigate).toHaveBeenCalled()
    await expect(outcome).resolves.toEqual(started ? { outcome: 'started', runId: 'run' } : { outcome: 'cancelled' })
  })
})
