// The move sheet and the App SDK call that opens it.
//
// Pinned here: `requestMemoryMove()` moves nothing by itself (no host or a bad target
// -> 'unavailable'); the sheet plans first (a dry run), and only the owner's press
// starts the run; a finished move answers 'moved'.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { api } from '../api/client'
import type { MemoryMove, MemoryProviderOverview } from '../api/client/memory'
import { requestMemoryMove } from '../app-sdk/memoryMove'
import { MemoryMoveSheetHost } from './MemoryMoveSheet'
import { i18nT } from '../i18n/t'

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
  providers: [],
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
    await screen.findByRole('button', { name: i18nT('pages.overview.memoryMove.rollback') })
    const closes = screen.getAllByRole('button', { name: i18nT('pages.overview.memoryProvider.close') })
    fireEvent.click(closes[closes.length - 1])
    await expect(outcome).resolves.toBe('moved')
  })
})
