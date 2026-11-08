//
// Contract under test: the usage page must not render a confident zero when
// the sessions scan did not load transcripts (#6733). A refused transcript file
// is counted in refusedTranscripts. On a Windows roaming-profile (UNC) home the
// path validator refuses the session directories themselves, so the backend
// reports total=0 with a positive unreadableSessionDirs count. The page has to
// say the counts are incomplete instead of showing 0 as a fact.
//
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { NormalizedUsage } from '../providers'

// Build a NormalizedUsage with controllable did-not-load counts. Everything else
// is a zeroed baseline -- the silent-failure shape the fix targets.
function usage(refusedTranscripts: number, unreadableSessionDirs = 0): NormalizedUsage {
  const period = { sessions: 0, messages: 0, toolCalls: 0 }
  return {
    sessions: {
      total: 0,
      today: period,
      thisWeek: period,
      thisMonth: period,
      avgMsgsPerSession: 0,
      refusedTranscripts,
      unreadableSessionDirs,
      dailyHistory: [],
    },
    billing: null,
  }
}

let current: NormalizedUsage = usage(0)

vi.mock('../providers', () => ({
  useProvider: () => ({
    id: 'acp',
    displayName: 'Kiro',
    capabilities: { usageBilling: true },
    fetchUsage: () => Promise.resolve(current),
  }),
}))
vi.mock('../api/client', () => ({
  api: {
    kirocrewConfig: vi.fn().mockResolvedValue({ agent: { acp_backend: 'kiro' } }),
    usageSeries: vi.fn().mockResolvedValue({ dates: [], series: [], total: 0, truncated: false, dropped_rows: 0, complete_from: null }),
  },
}))
// The Usage tab's spend chart colours its layers from the session palette,
// which reads the theme context; a fixed palette keeps these tests on the tab's
// own behaviour.
vi.mock('../hooks/useSessionPalette', () => ({
  useSessionPalette: () => ({ paletteColors: ['#ff0000', '#00ff00', '#0000ff'] }),
}))

// UsageTab is a default export.
import UsageTab from '../pages/overview/UsageTab'

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <UsageTab />
    </QueryClientProvider>,
  )
}

afterEach(() => cleanup())

describe('UsageTab refused-transcript warning (#6733)', () => {
  it('shows the incomplete-counts warning when transcripts were refused', async () => {
    current = usage(7)
    mount()
    // The English catalog string names the actor and leads with "couldn't load".
    await waitFor(() => {
      expect(screen.getByText(/couldn't load/i)).toBeInTheDocument()
    })
    expect(screen.getByText(/incomplete/i)).toBeInTheDocument()
    // The count is interpolated into the message.
    expect(screen.getByText(/\b7\b/)).toBeInTheDocument()
  })

  it('shows no warning when nothing was refused', async () => {
    current = usage(0)
    mount()
    // The session activity card is the arrival signal for a settled render.
    await waitFor(() => {
      expect(screen.getByText(/Session Activity/i)).toBeInTheDocument()
    })
    expect(screen.queryByText(/couldn't load/i)).toBeNull()
    expect(screen.queryByTestId('usage-unreadable-session-dirs')).toBeNull()
  })

  it('says the chat history could not be read, with no count, when a session directory did not load', async () => {
    // A refused directory hides an unknown number of chats, so the directory
    // count must not be rendered as a number of chats.
    current = usage(0, 2)
    mount()
    const notice = await screen.findByTestId('usage-unreadable-session-dirs')
    expect(notice).toHaveTextContent(/couldn't read your chat history/i)
    expect(notice).toHaveTextContent(/incomplete/i)
    expect(notice.textContent).not.toMatch(/\d/)
    expect(screen.queryByText(/couldn't load/i)).toBeNull()
  })

  it('shows only the directory warning when transcripts and directories both did not load', async () => {
    current = usage(3, 1)
    mount()
    const notice = await screen.findByTestId('usage-unreadable-session-dirs')
    expect(notice.textContent).not.toMatch(/\d/)
    expect(screen.queryByText(/couldn't load/i)).toBeNull()
  })
})
