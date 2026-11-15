// The move sheet and the App SDK call that opens it.
//
// Pinned here: `requestMemoryMove()` moves nothing by itself (no host or a bad target
// -> 'unavailable'); the sheet plans first (a dry run), and only the owner's press
// starts the run; a finished move answers 'moved', a copy still going when the sheet
// closes answers 'started', and a close after the owner pressed Undo answers 'cancelled'.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { api } from '../api/client'
import type { MemoryMove, MemoryProviderOverview } from '../api/client/memory'
import { requestMemoryMove } from '../app-sdk/memoryMove'
import { MemoryMoveSheetHost } from './MemoryMoveSheet'
import { i18nT } from '../i18n/t'
import { consumeChatHandoff, installSoftNavigate, setGatewayConnected, __resetNavSeamForTests } from '../utils/errorReport'

const TARGET = { from: 'builtin', to: 'app:fixture/fixture' }

function job(over: Partial<MemoryMove>): MemoryMove {
  return {
    id: 'p',
    from: TARGET.from,
    to: TARGET.to,
    store: 'default',
    dry_run: true,
    state: 'planned',
    checkpoint: { cursor: null, listed: 2 },
    by_kind: { rule: 2 },
    outcomes: { would_write: 2 },
    skipped_kinds: {},
    notes: [],
    verify: {},
    rolled_back: 0,
    error: '',
    samples: { rule: [{ id: 'a', kind: 'rule', text: 'Use tabs.', planned: 'would_write' }] },
    ...over,
  }
}

const OVERVIEW = {
  mode: 'builtin',
  id: '',
  switched_at: 0,
  stop_learning_ack: false,
  builtin: { id: 'builtin', display_name: 'Crew memory' },
  providers: [{ id: 'app:fixture/fixture', display_name: 'Fixture memory' }],
  shared_crew: false,
  policy_allows: true,
  active: { mode: 'builtin', id: 'builtin', unavailable: '' },
} as MemoryProviderOverview

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryMoveSheetHost />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  __resetNavSeamForTests()
  vi.restoreAllMocks()
})

describe('requestMemoryMove', () => {
  it('answers unavailable with no host and for a target outside the contract', async () => {
    await expect(requestMemoryMove(TARGET)).resolves.toBe('unavailable')
    mount()
    await expect(requestMemoryMove({ from: 'builtin', to: 'builtin' })).resolves.toBe('unavailable')
    await expect(requestMemoryMove({ from: 'builtin', to: 'https://elsewhere' })).resolves.toBe('unavailable')
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('plans first and runs only on the owner press', async () => {
    vi.spyOn(api, 'memoryProvider').mockResolvedValue(OVERVIEW)
    const start = vi
      .spyOn(api, 'startMemoryMove')
      .mockResolvedValueOnce(job({}))
      .mockResolvedValueOnce(job({ id: 'r', dry_run: false, state: 'running' }))
    vi.spyOn(api, 'memoryMove').mockImplementation(async id =>
      id === 'p' ? job({}) : job({ id: 'r', dry_run: false, state: 'done', outcomes: { inserted: 2 } }),
    )
    mount()
    let outcome: Promise<string> | undefined
    act(() => {
      outcome = requestMemoryMove(TARGET)
    })
    await screen.findByTestId('memory-move-sheet')
    await waitFor(() => expect(start).toHaveBeenCalledWith({ ...TARGET, dry_run: true }))
    expect(start).toHaveBeenCalledTimes(1)
    const run = await screen.findByRole('button', { name: i18nT('pages.overview.memoryMove.run') })
    await waitFor(() => expect(run).not.toBeDisabled())
    fireEvent.click(run)
    await waitFor(() => expect(start).toHaveBeenLastCalledWith(TARGET))
    // Run start -> run query poll -> done row: a bounded wait past the 1s default.
    await screen.findByRole('button', { name: i18nT('pages.overview.memoryMove.rollback', { to: 'Fixture memory' }) }, { timeout: 5000 })
    const closes = screen.getAllByRole('button', { name: i18nT('pages.overview.memoryProvider.close') })
    fireEvent.click(closes[closes.length - 1])
    await expect(outcome).resolves.toBe('moved')
  })

  it('holds Copy into a remote provider until the owner consents, and sends that consent', async () => {
    const remote = {
      ...OVERVIEW,
      providers: [{ id: 'app:fixture/fixture', display_name: 'Fixture memory', capabilities: { egress: 'remote' }, egress_consented: false }],
    } as MemoryProviderOverview
    vi.spyOn(api, 'memoryProvider').mockResolvedValue(remote)
    const start = vi
      .spyOn(api, 'startMemoryMove')
      .mockResolvedValueOnce(job({}))
      .mockResolvedValueOnce(job({ id: 'r', dry_run: false, state: 'running' }))
    vi.spyOn(api, 'memoryMove').mockImplementation(async id =>
      id === 'p' ? job({}) : job({ id: 'r', dry_run: false, state: 'done', outcomes: { inserted: 2 } }),
    )
    mount()
    act(() => {
      void requestMemoryMove(TARGET)
    })
    const consent = await screen.findByLabelText(
      i18nT('pages.overview.memoryProvider.egress_consent', { name: 'Fixture memory' }),
    )
    const run = await screen.findByRole('button', { name: i18nT('pages.overview.memoryMove.run') })
    await waitFor(() => expect(start).toHaveBeenCalledWith({ ...TARGET, dry_run: true }))
    expect(run).toBeDisabled()
    fireEvent.click(consent)
    await waitFor(() => expect(run).not.toBeDisabled())
    fireEvent.click(run)
    await waitFor(() => expect(start).toHaveBeenLastCalledWith({ ...TARGET, egress_consent: true }))
  })

  it('says the copy finished and offers no undo when it inserted nothing', async () => {
    vi.spyOn(api, 'memoryProvider').mockResolvedValue(OVERVIEW)
    vi.spyOn(api, 'startMemoryMove')
      .mockResolvedValueOnce(job({}))
      .mockResolvedValueOnce(job({ id: 'r', dry_run: false, state: 'running' }))
    vi.spyOn(api, 'memoryMove').mockImplementation(async id =>
      id === 'p' ? job({}) : job({ id: 'r', dry_run: false, state: 'done', outcomes: { deduped: 7 } }),
    )
    mount()
    act(() => {
      void requestMemoryMove(TARGET)
    })
    const run = await screen.findByRole('button', { name: i18nT('pages.overview.memoryMove.run') })
    await waitFor(() => expect(run).not.toBeDisabled())
    fireEvent.click(run)
    await screen.findByText(i18nT('pages.overview.memoryMove.done', { n: '0', m: '7' }))
    expect(
      screen.queryByRole('button', { name: i18nT('pages.overview.memoryMove.rollback', { to: 'Fixture memory' }) }),
    ).toBeNull()
  })

  it('holds Copy and names the raw provider while the overview cannot load', async () => {
    vi.spyOn(api, 'memoryProvider').mockRejectedValue(new Error('offline'))
    vi.spyOn(api, 'startMemoryMove').mockResolvedValue(job({}))
    vi.spyOn(api, 'memoryMove').mockResolvedValue(job({}))
    mount()
    act(() => {
      void requestMemoryMove(TARGET)
    })
    const run = await screen.findByRole('button', { name: i18nT('pages.overview.memoryMove.run') })
    await waitFor(() => expect(api.memoryMove).toHaveBeenCalled())
    expect(run).toBeDisabled()
    const unknown = TARGET.from === 'builtin' ? TARGET.to : TARGET.from
    expect(screen.getByTestId('memory-move-sheet').textContent).toContain(unknown)
  })

  it('answers cancelled, not moved, after the owner rolls the move back', async () => {
    vi.spyOn(api, 'memoryProvider').mockResolvedValue(OVERVIEW)
    vi.spyOn(api, 'startMemoryMove')
      .mockResolvedValueOnce(job({}))
      .mockResolvedValueOnce(job({ id: 'r', dry_run: false, state: 'running' }))
    vi.spyOn(api, 'memoryMove').mockImplementation(async id =>
      id === 'p' ? job({}) : job({ id: 'r', dry_run: false, state: 'rolled_back', rolled_back: 2 }),
    )
    mount()
    let outcome: Promise<string> | undefined
    act(() => {
      outcome = requestMemoryMove(TARGET)
    })
    const run = await screen.findByRole('button', { name: i18nT('pages.overview.memoryMove.run') })
    await waitFor(() => expect(run).not.toBeDisabled())
    fireEvent.click(run)
    await screen.findByText(i18nT('pages.overview.memoryMove.rolled_back', { n: '2', to: 'Fixture memory' }))
    const closes = screen.getAllByRole('button', { name: i18nT('pages.overview.memoryProvider.close') })
    fireEvent.click(closes[closes.length - 1])
    await expect(outcome).resolves.toBe('cancelled')
  })

  it('answers started when the owner closes the sheet while the copy is still going', async () => {
    vi.spyOn(api, 'memoryProvider').mockResolvedValue(OVERVIEW)
    vi.spyOn(api, 'startMemoryMove')
      .mockResolvedValueOnce(job({}))
      .mockResolvedValueOnce(job({ id: 'r', dry_run: false, state: 'running' }))
    vi.spyOn(api, 'memoryMove').mockImplementation(async id =>
      id === 'p' ? job({}) : job({ id: 'r', dry_run: false, state: 'running', outcomes: {} }),
    )
    mount()
    let outcome: Promise<string> | undefined
    act(() => {
      outcome = requestMemoryMove(TARGET)
    })
    const run = await screen.findByRole('button', { name: i18nT('pages.overview.memoryMove.run') })
    await waitFor(() => expect(run).not.toBeDisabled())
    fireEvent.click(run)
    await screen.findByText(i18nT('pages.overview.memoryMove.running'))
    const closes = screen.getAllByRole('button', { name: i18nT('pages.overview.memoryProvider.close') })
    fireEvent.click(closes[closes.length - 1])
    await expect(outcome).resolves.toBe('started')
  })

  it.each(['pending', 'accepted'])('answers started when Resume is %s and the paused job has not refreshed', async status => {
    vi.spyOn(api, 'memoryProvider').mockResolvedValue(OVERVIEW)
    vi.spyOn(api, 'startMemoryMove')
      .mockResolvedValueOnce(job({}))
      .mockResolvedValueOnce(job({ id: 'r', dry_run: false, state: 'running' }))
    const resume = vi.spyOn(api, 'resumeMemoryMove').mockImplementation(() =>
      status === 'pending' ? new Promise(() => {}) : Promise.resolve(job({ id: 'r', dry_run: false, state: 'running' })),
    )
    const read = vi.spyOn(api, 'memoryMove').mockImplementation(async id => {
      if (id === 'p') return job({})
      // Hold the refresh so closing still sees the paused job after Resume is accepted.
      if (resume.mock.calls.length) return new Promise(() => {})
      return job({ id: 'r', dry_run: false, state: 'paused', outcomes: {} })
    })
    mount()
    let outcome: Promise<string> | undefined
    act(() => {
      outcome = requestMemoryMove(TARGET)
    })
    const run = await screen.findByRole('button', { name: i18nT('pages.overview.memoryMove.run') })
    await waitFor(() => expect(run).not.toBeDisabled())
    fireEvent.click(run)
    const resumeButton = await screen.findByRole('button', { name: i18nT('pages.overview.memoryMove.resume') })
    read.mockClear()
    fireEvent.click(resumeButton)
    await waitFor(() => expect(resume).toHaveBeenCalledWith('r'))
    if (status === 'pending') {
      await waitFor(() => expect(resumeButton).toHaveAttribute('aria-busy', 'true'))
    } else {
      await waitFor(() => expect(read).toHaveBeenCalledWith('r'))
      await waitFor(() => expect(resumeButton).not.toHaveAttribute('aria-busy', 'true'))
    }
    const closes = screen.getAllByRole('button', { name: i18nT('pages.overview.memoryProvider.close') })
    fireEvent.click(closes[closes.length - 1])
    await expect(outcome).resolves.toBe('started')
  })

  it('answers cancelled when the owner closes right after pressing Undo, before the job refreshes', async () => {
    vi.spyOn(api, 'memoryProvider').mockResolvedValue(OVERVIEW)
    vi.spyOn(api, 'startMemoryMove')
      .mockResolvedValueOnce(job({}))
      .mockResolvedValueOnce(job({ id: 'r', dry_run: false, state: 'running' }))
    vi.spyOn(api, 'memoryMove').mockImplementation(async id =>
      id === 'p' ? job({}) : job({ id: 'r', dry_run: false, state: 'done', outcomes: { inserted: 2 } }),
    )
    // The undo is accepted but has not answered yet, so the cached job still reads 'done'.
    vi.spyOn(api, 'rollbackMemoryMove').mockReturnValue(new Promise(() => {}))
    mount()
    let outcome: Promise<string> | undefined
    act(() => {
      outcome = requestMemoryMove(TARGET)
    })
    const run = await screen.findByRole('button', { name: i18nT('pages.overview.memoryMove.run') })
    await waitFor(() => expect(run).not.toBeDisabled())
    fireEvent.click(run)
    const undo = await screen.findByRole('button', {
      name: i18nT('pages.overview.memoryMove.rollback', { to: 'Fixture memory' }),
    })
    fireEvent.click(undo)
    await waitFor(() => expect(api.rollbackMemoryMove).toHaveBeenCalledWith('r'))
    const closes = screen.getAllByRole('button', { name: i18nT('pages.overview.memoryProvider.close') })
    fireEvent.click(closes[closes.length - 1])
    await expect(outcome).resolves.toBe('cancelled')
  })
})

describe('MemoryMoveSheet failures', () => {
  it('shows an undo the gateway refused instead of leaving the finished copy as it was', async () => {
    vi.spyOn(api, 'memoryProvider').mockResolvedValue(OVERVIEW)
    vi.spyOn(api, 'startMemoryMove')
      .mockResolvedValueOnce(job({}))
      .mockResolvedValueOnce(job({ id: 'r', dry_run: false, state: 'done', outcomes: { inserted: 2 } }))
    vi.spyOn(api, 'memoryMove').mockImplementation(async id =>
      id === 'p' ? job({}) : job({ id: 'r', dry_run: false, state: 'done', outcomes: { inserted: 2 } }),
    )
    vi.spyOn(api, 'rollbackMemoryMove').mockRejectedValue(new Error('provider unavailable'))
    mount()
    act(() => {
      void requestMemoryMove(TARGET)
    })
    const run = await screen.findByRole('button', { name: i18nT('pages.overview.memoryMove.run') })
    await waitFor(() => expect(run).not.toBeDisabled())
    fireEvent.click(run)
    fireEvent.click(await screen.findByRole('button', { name: i18nT('pages.overview.memoryMove.rollback', { to: 'Fixture memory' }) }))
    await screen.findByText(i18nT('pages.overview.memoryMove.failed'))
  })

  it('shows a plan whose polling failed instead of planning forever', async () => {
    vi.spyOn(api, 'memoryProvider').mockResolvedValue(OVERVIEW)
    vi.spyOn(api, 'startMemoryMove').mockResolvedValue(job({ state: 'planning' }))
    vi.spyOn(api, 'memoryMove').mockRejectedValue(new Error('gateway down'))
    mount()
    act(() => {
      void requestMemoryMove(TARGET)
    })
    await screen.findByText(i18nT('pages.overview.memoryMove.failed'))
    expect(screen.queryByText(i18nT('pages.overview.memoryMove.planning'))).toBeNull()
  })
})

describe('MemoryProviderPicker unfinished copies', () => {
  it('probes health despite a cached unprobed overview and refreshes on prefix invalidation', async () => {
    const { default: MemoryProviderPicker } = await import('../pages/settings/MemoryProviderPicker')
    const { MEMORY_PROVIDER_QUERY_KEY } = await import('./MemoryProviderSheet')
    const detail = 'The health request timed out.'
    const overview: MemoryProviderOverview = {
      ...OVERVIEW,
      mode: 'replace',
      id: TARGET.to,
      active: { mode: 'replace', id: TARGET.to, unavailable: '' },
    }
    const probe = vi.spyOn(api, 'memoryProvider').mockResolvedValue({
      ...overview,
      active: { ...overview.active, health: { ok: false, detail } },
    })
    vi.spyOn(api, 'memoryMoves').mockResolvedValue({ migrations: [] })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    client.setQueryData(MEMORY_PROVIDER_QUERY_KEY, overview)
    render(
      <QueryClientProvider client={client}>
        <MemoryProviderPicker />
      </QueryClientProvider>,
    )
    await waitFor(() => expect(probe).toHaveBeenCalledWith(true))
    expect(await screen.findByRole('alert')).toHaveTextContent(detail)
    expect(probe).toHaveBeenCalledTimes(1)
    await act(() => client.invalidateQueries({ queryKey: MEMORY_PROVIDER_QUERY_KEY }))
    expect(probe).toHaveBeenCalledTimes(2)
    expect(probe).toHaveBeenLastCalledWith(true)
  })

  it('shows a failed health probe and hands its detail to the agent', async () => {
    const { default: MemoryProviderPicker } = await import('../pages/settings/MemoryProviderPicker')
    const detail = 'The health request timed out.'
    vi.spyOn(api, 'memoryProvider').mockResolvedValue({
      ...OVERVIEW,
      mode: 'replace',
      id: TARGET.to,
      active: { mode: 'replace', id: TARGET.to, unavailable: '', health: { ok: false, detail } },
    })
    vi.spyOn(api, 'memoryMoves').mockResolvedValue({ migrations: [] })
    const navigate = vi.fn()
    installSoftNavigate(navigate)
    setGatewayConnected(true)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <MemoryProviderPicker />
      </QueryClientProvider>,
    )
    const notice = await screen.findByRole('alert')
    expect(notice).toHaveTextContent(detail)
    expect(api.memoryProvider).toHaveBeenCalledWith(true)
    fireEvent.click(within(notice).getByRole('button', { name: i18nT('components.askAgent.ask_the_agent') }))
    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/chat'))
    expect(consumeChatHandoff()).toContain(detail)
  })

  it('says a failed connect once', async () => {
    const { default: MemoryProviderPicker } = await import('../pages/settings/MemoryProviderPicker')
    const failing = { id: 'app:bridge/b', display_name: 'Bridge', blocked_reason: 'the provider did not answer connect (invalid)', connect_failed: true }
    vi.spyOn(api, 'memoryProvider').mockResolvedValue({ ...OVERVIEW, providers: [...OVERVIEW.providers, failing] } as MemoryProviderOverview)
    vi.spyOn(api, 'memoryMoves').mockResolvedValue({ migrations: [] })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <MemoryProviderPicker />
      </QueryClientProvider>,
    )
    const text = i18nT('pages.overview.memoryProvider.blocked', { reason: failing.blocked_reason })
    expect(await screen.findAllByText(text)).toHaveLength(1)
  })

  it('lists a paused copy and reopens it to resume, without planning a new one', async () => {
    const { default: MemoryProviderPicker } = await import('../pages/settings/MemoryProviderPicker')
    vi.spyOn(api, 'memoryProvider').mockResolvedValue(OVERVIEW)
    const paused = job({ id: 'r', dry_run: false, state: 'paused', outcomes: { inserted: 1 } })
    vi.spyOn(api, 'memoryMoves').mockResolvedValue({ migrations: [job({}), paused] })
    vi.spyOn(api, 'memoryMove').mockResolvedValue(paused)
    const start = vi.spyOn(api, 'startMemoryMove')
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <MemoryProviderPicker />
      </QueryClientProvider>,
    )
    const list = await screen.findByTestId('memory-move-unfinished')
    expect(list.textContent).toContain(i18nT('pages.overview.memoryProvider.move_paused'))
    fireEvent.click(screen.getByRole('button', { name: i18nT('pages.overview.memoryProvider.unfinished_open') }))
    await screen.findByRole('button', { name: i18nT('pages.overview.memoryMove.resume') })
    // It wrote an item before it paused, so its undo is offered beside Resume.
    screen.getByRole('button', { name: i18nT('pages.overview.memoryMove.rollback', { to: 'Fixture memory' }) })
    expect(start).not.toHaveBeenCalled()
  })

  it('says so when the list of unfinished copies cannot be read', async () => {
    const { default: MemoryProviderPicker } = await import('../pages/settings/MemoryProviderPicker')
    vi.spyOn(api, 'memoryProvider').mockResolvedValue(OVERVIEW)
    vi.spyOn(api, 'memoryMoves').mockRejectedValue(new Error('offline'))
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <MemoryProviderPicker />
      </QueryClientProvider>,
    )
    expect(await screen.findByText(i18nT('pages.overview.memoryProvider.moves_load_failed'))).toBeTruthy()
  })
})

it('settles and removes the move sheet when an error is handed to chat', async () => {
  vi.spyOn(api, 'memoryProvider').mockResolvedValue(OVERVIEW)
  vi.spyOn(api, 'startMemoryMove').mockRejectedValue(new Error('offline'))
  const navigate = vi.fn()
  installSoftNavigate(navigate)
  setGatewayConnected(true)
  mount()
  let outcome: Promise<string> | undefined
  act(() => { outcome = requestMemoryMove(TARGET) })
  await screen.findByText(i18nT('pages.overview.memoryMove.failed'))
  fireEvent.click(screen.getByRole('button', { name: i18nT('components.askAgent.ask_the_agent') }))
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  expect(navigate).toHaveBeenCalled()
  await expect(outcome).resolves.toBe('cancelled')
})
