//
// TodayTab (Settings > Overview > Today drill-in) and the Overview Today
// summary card. Covers the states a user can land in: sessions grouped by
// folder with an open-on-click row, no sessions yet, the memory half empty
// with the idle-hours explanation from the shared setting, memory entries
// rendered newest first, and a failed sessions read surfacing as an
// ErrorNotice rather than an empty list.
//
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import TodayTab from '../pages/overview/TodayTab'
import { api } from '../api/client'
import type { RootState } from '../store'

vi.mock('../api/client', async (orig) => {
  const actual = await orig<typeof import('../api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      sessions: vi.fn(),
      chatFolders: vi.fn(),
      memoryHistory: vi.fn(),
      memorySettings: vi.fn(),
    },
  }
})

const mockedNavigate = vi.fn()
vi.mock('react-router-dom', async (orig) => {
  const actual = await orig<typeof import('react-router-dom')>()
  return { ...actual, useNavigate: () => mockedNavigate }
})

// The two doors into a session are thunks that fetch the transcript; here
// they are inert actions so the test asserts WHICH door was used, not the
// transcript load behind it.
vi.mock('../store/chatSlice', async (orig) => {
  const actual = await orig<typeof import('../store/chatSlice')>()
  return {
    ...actual,
    switchSlot: vi.fn(() => ({ type: 'test/switchSlot' })),
    resumeFromHistory: vi.fn(() => ({ type: 'test/resumeFromHistory' })),
  }
})
import { resumeFromHistory, switchSlot } from '../store/chatSlice'

const sessions = api.sessions as unknown as ReturnType<typeof vi.fn>
const chatFolders = api.chatFolders as unknown as ReturnType<typeof vi.fn>
const memoryHistory = api.memoryHistory as unknown as ReturnType<typeof vi.fn>
const memorySettings = api.memorySettings as unknown as ReturnType<typeof vi.fn>

const nowIso = () => new Date().toISOString()
const nowEpoch = () => Math.floor(Date.now() / 1000)

function storeWithSlots(slots: Partial<RootState['dashboard']['slots'][number]>[]) {
  return createTestStore({
    dashboard: {
      status: null,
      connected: true,
      slots,
      refreshTrigger: 0,
    } as unknown as RootState['dashboard'],
  })
}

function mount(slots: Partial<RootState['dashboard']['slots'][number]>[] = []) {
  const onOpenMemory = vi.fn()
  const view = renderWithProviders(<TodayTab onOpenMemory={onOpenMemory} />, { store: storeWithSlots(slots) })
  return { ...view, onOpenMemory }
}

beforeEach(() => {
  sessions.mockReset().mockResolvedValue({ sessions: [] })
  chatFolders.mockReset().mockResolvedValue([{ id: 'f1', name: 'Website', order: 0 }])
  memoryHistory.mockReset().mockResolvedValue({ content: '' })
  memorySettings.mockReset().mockResolvedValue({ history_idle_hours: 3 })
  mockedNavigate.mockReset()
})
afterEach(() => cleanup())

describe('TodayTab', () => {
  it('groups today’s sessions under their folder and opens a live slot on click', async () => {
    sessions.mockResolvedValue({ sessions: [{ key: 'old-1', title: 'Babysit the PRs', modified: nowEpoch(), folder_id: 'f1' }] })
    mount([
      { key: 'live-1', title: 'Overview Today card', messages: 41, running: true, last_turn_ts: nowIso(), folder_id: 'f1' },
      { key: 'live-2', title: 'Cron timezone question', messages: 3, running: false, last_turn_ts: nowIso() },
    ])

    expect(await screen.findByText('Overview Today card')).toBeInTheDocument()
    await screen.findByText('Babysit the PRs')
    // Folder header for the filed rows, "Unfiled" for the rest.
    expect(screen.getByText('Website')).toBeInTheDocument()
    expect(screen.getByText('Unfiled')).toBeInTheDocument()
    // Exact counts only for live slots.
    expect(screen.getByText(/41 messages/)).toBeInTheDocument()
    expect(screen.getAllByTestId('today-session-row')).toHaveLength(3)

    // The date the request was made with is the browser-local day.
    const today = new Date()
    const key = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`
    expect(memoryHistory).toHaveBeenCalledWith(undefined, key)

    fireEvent.click(screen.getByText('Overview Today card'))
    expect(switchSlot).toHaveBeenCalledWith({ key: 'live-1', announceOnMissing: true })
    expect(mockedNavigate).toHaveBeenCalledWith('/chat')

    fireEvent.click(screen.getByText('Babysit the PRs'))
    expect(resumeFromHistory).toHaveBeenCalledWith({ key: 'old-1', title: 'Babysit the PRs' })
    expect(mockedNavigate).toHaveBeenCalledTimes(2)
  })

  it('shows the no-sessions empty state and the idle-hours memory explanation', async () => {
    memorySettings.mockResolvedValue({ history_idle_hours: 2 })
    mount()
    expect(await screen.findByTestId('today-sessions-empty')).toBeInTheDocument()
    expect(screen.getByText('No sessions yet today')).toBeInTheDocument()
    // The empty state ends in an action, like the memory half's Open Memory.
    fireEvent.click(screen.getByTestId('today-go-to-chat'))
    expect(mockedNavigate).toHaveBeenCalledWith('/chat')
    const memoryEmpty = await screen.findByTestId('today-memory-empty')
    expect(memoryEmpty).toBeInTheDocument()
    await waitFor(() =>
      expect(screen.getByTestId('today-memory-empty-subtitle')).toHaveTextContent(
        'Sessions are summarized into memory after 2 hours of inactivity.',
      ),
    )
  })

  it('states the count basis as visible text, scoped to what is on screen', async () => {
    mount([{ key: 'live-1', title: 'Overview Today card', messages: 41, running: true, last_turn_ts: nowIso() }])
    expect(await screen.findByText('Overview Today card')).toBeInTheDocument()
    // The card carries the two-sentence basis; the drill-in has no Usage card
    // beside it, so only the scope sentence is shown here.
    const basis = screen.getByTestId('today-sessions-basis')
    expect(basis).toHaveTextContent('Sessions with any activity today, including ones started earlier.')
    expect(basis).not.toHaveTextContent('Usage card')
  })

  it('marks a running row by shape as well as color', async () => {
    mount([
      { key: 'live-1', title: 'Overview Today card', messages: 41, running: true, last_turn_ts: nowIso() },
      { key: 'live-2', title: 'Cron timezone question', messages: 3, running: false, last_turn_ts: nowIso() },
    ])
    expect(await screen.findByText('Overview Today card')).toBeInTheDocument()
    const running = screen.getByRole('img', { name: 'Running' })
    // Filled and ringed against a hollow circle: the difference survives
    // color-blind viewing, where `bg-ok` vs a grey dot would not.
    expect(running.className).toContain('ring-2')
    expect(running.className).not.toContain('border ')
    const idle = screen.getByText('Cron timezone question').closest('[data-testid="today-session-row"]')!.firstElementChild as HTMLElement
    expect(idle.className).toContain('border')
    expect(idle.className).not.toContain('bg-ok')
    expect(idle).not.toHaveAttribute('role')
  })

  it('renders the day file as entries, newest first, and hands off to the Memory view', async () => {
    memoryHistory.mockResolvedValue({
      content: '# 2026-09-15\n\n#### 07:58 PDT\nFirst entry.\n\n#### 09:12 PDT\nSecond entry.\n',
    })
    const { onOpenMemory } = mount()
    const entries = await screen.findAllByTestId('today-memory-entry')
    expect(entries.map(e => e.textContent)).toEqual(['09:12 PDTSecond entry.', '07:58 PDTFirst entry.'])
    fireEvent.click(screen.getByTestId('today-open-memory'))
    expect(onOpenMemory).toHaveBeenCalledTimes(1)
  })

  it('surfaces a failed sessions read as an error notice, not an empty list', async () => {
    sessions.mockRejectedValue(new Error('sessions unavailable'))
    mount()
    const notice = await screen.findByTestId('today-sessions-error')
    expect(notice).toHaveTextContent(/Today's sessions could not be read/)
    expect(notice).toHaveTextContent('sessions unavailable')
    expect(screen.queryByTestId('today-sessions-empty')).not.toBeInTheDocument()
  })
})
