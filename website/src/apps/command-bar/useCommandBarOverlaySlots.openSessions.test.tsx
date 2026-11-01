import { describe, it, expect, vi } from 'vitest'
import { createElement, useState, type ReactNode } from 'react'
import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import type { Result, ResourceProvider } from '../../components/commandPalette/types'
import { useCommandBarOverlaySlots } from './useCommandBarOverlaySlots'
import type { RankedRow } from './rootIndex'

/**
 * Issue #21290: the typed Sessions search ranks open sessions first, so the cached
 * rows must be re-ranked when the open-session set changes -- not served from the
 * 15-second cache in the order they had before a session was opened. While that
 * re-rank is in flight the rows already on screen stay; only a new query drops them.
 */

function row(id: string): Result {
  return { id, providerId: 'sessions', title: id, score: 0, indices: [] } as Result
}

function provider(search: ResourceProvider['search']): ResourceProvider {
  return { id: 'stub', label: 'stub', icon: null, search } as unknown as ResourceProvider
}

function renderSlots(search: ResourceProvider['search']) {
  const sessions = provider(search)
  const idle = provider(async () => [])
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children)
  const view = renderHook(
    ({ q, openSessionsKey }: { q: string; openSessionsKey: string }) =>
      useCommandBarOverlaySlots({
        scope: 'sessions',
        debounced: q,
        query: q,
        sessions,
        openSessionsKey,
        recentSessions: idle,
        artifacts: idle,
        folderSortMode: 'manual',
        folders: idle,
        runningMates: new Set<string>(),
        mates: idle,
        argCommand: null,
        ranked: [],
        selected: 0,
        setSelected: () => {},
      } as Parameters<typeof useCommandBarOverlaySlots>[0]),
    { wrapper, initialProps: { q: 'grid', openSessionsKey: '' } },
  )
  const titles = () =>
    view.result.current.slots.flatMap(s => (s.tag === 'result' ? [s.row.title] : []))
  return { ...view, titles }
}

/** A search whose answers resolve only when the test releases them, in call order. */
function gatedSearch(answers: string[][]) {
  const releases: Array<() => void> = []
  const search = vi.fn(
    () =>
      new Promise<Result[]>(resolve => {
        const answer = answers[releases.length]
        releases.push(() => resolve(answer.map(row)))
      }),
  )
  return { search, release: (i: number) => releases[i]() }
}

describe('useCommandBarOverlaySlots — open sessions re-rank cached search rows (#21290)', () => {
  it('ranks a session opened since the last search first, inside the cache window', async () => {
    let open = new Set<string>()
    const search = vi.fn(async (): Promise<Result[]> =>
      ['history', 'target'].sort((a, b) => Number(open.has(b)) - Number(open.has(a))).map(row),
    )
    const { rerender, titles } = renderSlots(search)
    await waitFor(() => expect(titles()).toEqual(['history', 'target']))

    // The user opens `target`; the same query is typed again well inside staleTime.
    open = new Set(['target'])
    rerender({ q: 'grid', openSessionsKey: 'target' })

    await waitFor(() => expect(titles()).toEqual(['target', 'history']))
    expect(search).toHaveBeenCalledTimes(2)
  })

  it('keeps the rows on screen while a change of the open set re-ranks them', async () => {
    const { search, release } = gatedSearch([['history', 'target'], ['target', 'history']])
    const { rerender, titles } = renderSlots(search)
    await waitFor(() => expect(search).toHaveBeenCalledTimes(1))
    release(0)
    await waitFor(() => expect(titles()).toEqual(['history', 'target']))

    // A slot opens in the background; the re-rank is in flight and not yet released.
    rerender({ q: 'grid', openSessionsKey: 'target' })
    await waitFor(() => expect(search).toHaveBeenCalledTimes(2))
    expect(titles()).toEqual(['history', 'target'])

    release(1)
    await waitFor(() => expect(titles()).toEqual(['target', 'history']))
  })

  it('keeps the keyboard selection on the same row when a re-rank moves it', async () => {
    const answers = [['history', 'target', 'other'], ['target', 'history', 'other']]
    let call = 0
    const search = vi.fn(async (): Promise<Result[]> => answers[Math.min(call++, 1)].map(row))
    const sessions = provider(search)
    const idle = provider(async () => [])
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client }, children)
    const view = renderHook(
      ({ q, openSessionsKey }: { q: string; openSessionsKey: string }) => {
        const [selected, setSelected] = useState(0)
        const out = useCommandBarOverlaySlots({
          scope: 'sessions', debounced: q, query: q, sessions, openSessionsKey,
          recentSessions: idle, artifacts: idle, folderSortMode: 'manual', folders: idle,
          runningMates: new Set<string>(), mates: idle, argCommand: null, ranked: [],
          selected, setSelected,
        } as Parameters<typeof useCommandBarOverlaySlots>[0])
        return { ...out, selected, setSelected }
      },
      { wrapper, initialProps: { q: 'grid', openSessionsKey: '' } },
    )
    const selectedTitle = () => {
      const slot = view.result.current.slots[view.result.current.selected]
      return slot?.tag === 'result' ? slot.row.title : undefined
    }
    await waitFor(() => expect(view.result.current.slots).toHaveLength(3))
    // The reader arrows down to `target`.
    act(() => view.result.current.setSelected(1))
    expect(selectedTitle()).toBe('target')

    // `target` opens in the background and moves to row 0: the highlight follows it.
    view.rerender({ q: 'grid', openSessionsKey: 'target' })
    await waitFor(() => expect(view.result.current.slots[0]?.key).toBe('target'))
    expect(selectedTitle()).toBe('target')
    expect(view.result.current.selected).toBe(0)
  })

  it('lets a keystroke at the root reset the selection, before the debounce lands', () => {
    const idle = provider(async () => [])
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client }, children)
    const rootRow = (id: string) => ({ id, title: id }) as unknown as RankedRow
    const view = renderHook(
      ({ query, ranked }: { query: string; ranked: RankedRow[] }) => {
        const [selected, setSelected] = useState(0)
        const out = useCommandBarOverlaySlots({
          scope: null, debounced: 'g', query, sessions: idle, openSessionsKey: '',
          recentSessions: idle, artifacts: idle, folderSortMode: 'manual', folders: idle,
          runningMates: new Set<string>(), mates: idle, argCommand: null, ranked,
          selected, setSelected,
        } as unknown as Parameters<typeof useCommandBarOverlaySlots>[0])
        return { ...out, selected, setSelected }
      },
      { wrapper, initialProps: { query: 'g', ranked: [rootRow('alpha'), rootRow('grid')] } },
    )
    act(() => view.result.current.setSelected(1))
    // The reader types `r`: the input resets the selection and the live ranking
    // moves `grid` to the top. The debounced query has not caught up yet.
    act(() => view.result.current.setSelected(0))
    view.rerender({ query: 'gr', ranked: [rootRow('grid'), rootRow('alpha')] })
    expect(view.result.current.selected).toBe(0)
    expect(view.result.current.slots[0]?.key).toBe('grid')
  })

  it('still drops the rows when the query itself changes', async () => {
    const { search, release } = gatedSearch([['history'], ['other']])
    const { rerender, titles } = renderSlots(search)
    await waitFor(() => expect(search).toHaveBeenCalledTimes(1))
    release(0)
    await waitFor(() => expect(titles()).toEqual(['history']))

    rerender({ q: 'gridx', openSessionsKey: '' })
    await waitFor(() => expect(search).toHaveBeenCalledTimes(2))
    expect(titles()).toEqual([])

    release(1)
    await waitFor(() => expect(titles()).toEqual(['other']))
  })
})
