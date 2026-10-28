/**
 * One rule for the person's session lists: a Crew Mode root's workers stay out of
 * every list that shows the person their sessions (the Sessions page and the
 * command palette's Recents here; the chat sidebar has its own test). The root
 * itself stays.
 */
import { describe, it, expect, vi } from 'vitest'
import { renderHook, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import SessionsPage from '../pages/SessionsPage'
import { selectPersonSlots } from '../store/dashboardSlice'
import { useRecentsProvider } from '../components/commandPalette/providers/recentsProvider'
import { renderWithProviders, createTestStore } from './helpers'
import type { ChatSlot } from '../types'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      chatTags: vi.fn().mockResolvedValue([]),
      sessions: vi.fn().mockResolvedValue({ sessions: [] }),
      crons: vi.fn().mockResolvedValue([]),
      chatFolders: vi.fn().mockResolvedValue([]),
    },
  }
})

const row = (key: string, title: string, extra: Partial<ChatSlot> = {}) =>
  ({ key, title, messages: 2, running: false, last_ts: '2026-10-21T03:00:00.000Z', ...extra }) as ChatSlot

const ROWS = [
  row('root', 'Welcome kit', { crew_mode_root: true } as Partial<ChatSlot>),
  row('w1', 'Welcome note', { crew_root: 'root' } as Partial<ChatSlot>),
  row('plain', 'Coffee names'),
]

function store() {
  const base = createTestStore()
  const state = base.getState()
  return createTestStore({ ...state, dashboard: { ...state.dashboard, slots: ROWS, slotsLoaded: true } })
}

describe('the person-list rule', () => {
  it('selectPersonSlots reads through crewModeView, inert on this part: every row, the worker too', () => {
    // The drop of a root's workers is turned on by the part that lands the Crew
    // panel, which lists them; until then a worker stays a row the person can reach.
    const keys = selectPersonSlots(store().getState()).map(s => s.key)
    expect(keys).toEqual(['root', 'w1', 'plain'])
  })

  it('the Sessions page reads the person rule (inert on this part: the worker is listed)', async () => {
    renderWithProviders(
      <Routes><Route path="/sessions" element={<SessionsPage />} /></Routes>,
      { store: store(), route: '/sessions' },
    )
    expect(await screen.findByText('Coffee names')).toBeInTheDocument()
    expect(screen.getByText('Welcome kit')).toBeInTheDocument()
    expect(screen.getByText('Welcome note')).toBeInTheDocument()
  })

  it("the command palette's Recents reads the person rule (inert on this part) and never doubles a live worker as an older row", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const s = store()
    const { result } = renderHook(() => useRecentsProvider(), {
      wrapper: ({ children }) => (
        <QueryClientProvider client={qc}><Provider store={s}><MemoryRouter>{children}</MemoryRouter></Provider></QueryClientProvider>
      ),
    })
    let titles: string[] = []
    await waitFor(async () => {
      titles = (await result.current.search('')).map(r => r.title)
      expect(titles).toContain('Coffee names')
    })
    expect(titles).toContain('Welcome kit')
    expect(titles.filter(t => t === 'Welcome note')).toHaveLength(1)
  })
})
