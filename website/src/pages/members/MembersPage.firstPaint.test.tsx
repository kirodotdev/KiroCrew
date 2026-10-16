import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useLayoutEffect } from 'react'
import { render, screen, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from '../../test/helpers'
import { ThemeProvider } from '../../hooks/useTheme'
import { MEMBERS_ROSTER_QUERY_KEY } from '../../api/membersQuery'
import { TEAMS_QUERY_KEY } from '../../api/teamsQuery'
import { defaultAgentQuery } from '../../api/defaultAgentQuery'

/* The first frame of the Crewmates page must already be its final layout.
 * A switch back to this tab finds the roster, the team list and the default
 * crew in the query cache, yet the crewmate the page opens is opened by an
 * effect, which runs after paint. The first frame used to read that as
 * "nothing open" and draw the full roster column, and the next frame folded
 * it: a visible flash on every tab switch. */

let resolveTeams: (v: { teams: { id: string; name: string; members: string[] }[] }) => void = () => {}

vi.mock('../../api/client', () => ({
  api: {
    members: vi.fn(),
    teams: {
      list: vi.fn(() => new Promise((resolve) => { resolveTeams = resolve })),
    },
    crewBoard: vi.fn(() => Promise.reject(Object.assign(new Error('no_ledger'), { status: 404 }))),
    memberRecap: vi.fn(() => Promise.reject(new Error('no recap in this test'))),
    memberThread: vi.fn((slug: string) =>
      Promise.resolve({ slot_key: 'member-' + slug, slug, member: slug, created: true }),
    ),
    memberActivity: vi.fn(() => Promise.resolve({ slug: '', member: '', capped: false, entries: [] })),
    crons: vi.fn(() => Promise.resolve({ jobs: [] })),
    webhooks: vi.fn(() => Promise.resolve({ tokens: [] })),
    defaultAgent: vi.fn(() => Promise.resolve({ default_agent: '' })),
    autonudgeList: vi.fn(() => Promise.resolve({ enabled: true, loops: [] })),
    autonudgeForSlot: vi.fn(() => Promise.resolve({ enabled: true, loop: null })),
    monitorForSlot: vi.fn(() => Promise.resolve({ enabled: true, monitor: null })),
  },
}))

vi.mock('../../components/ChatPane', () => ({
  default: ({ slotKey }: { slotKey: string }) => <div data-testid="chat-pane-stub">{slotKey}</div>,
}))

import { api } from '../../api/client'
import MembersPage from './MembersPage'

function row(name: string, lastActive: number) {
  return {
    name,
    slug: name,
    slot_key: '',
    running: false,
    kiro_agent: name,
    workspace: 'default',
    memory_store: 'default',
    model: '',
    source: 'kirocrew',
    starred: false,
    last_active_ts: lastActive,
  }
}

const ROSTER = [row('scribe', 300), row('radar', 200), row('pilot', 100)]

interface Frame {
  folded: boolean
  rows: string[]
  groups: number
  waiting: boolean
}

/** What the roster looks like in a committed DOM: whether its column is folded
 *  away (class `hidden`, no `flex` at any breakpoint), which rows it draws, and
 *  how many team groups. */
function snapshot(): Frame {
  const aside = document.querySelector('[data-testid="member-roster"]')
  const cls = (aside?.getAttribute('class') ?? '').split(/\s+/)
  return {
    folded: cls.includes('hidden') && !cls.includes('flex') && !cls.includes('md:flex'),
    rows: Array.from(document.querySelectorAll('[data-testid^="member-star-"]')).map((el) =>
      el.getAttribute('data-testid')!.replace('member-star-', ''),
    ),
    groups: document.querySelectorAll('[data-testid="team-group"]').length,
    waiting: !!document.querySelector('[data-testid="member-landing-pending"]'),
  }
}

/** Rendered AFTER the page as its sibling: its layout effect runs once the
 *  first commit's DOM is in place and before any passive effect (the page's
 *  landing effect is one) — exactly the frame the browser paints first. */
function FirstFrameProbe({ onFrame }: { onFrame: (f: Frame) => void }) {
  useLayoutEffect(() => { onFrame(snapshot()) }, []) // eslint-disable-line react-hooks/exhaustive-deps
  return null
}

function renderPage(seed: { members: unknown[]; teams?: unknown[]; defaultAgent?: string }) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 30_000 } } })
  queryClient.setQueryData(MEMBERS_ROSTER_QUERY_KEY, seed.members)
  if (seed.teams) queryClient.setQueryData(TEAMS_QUERY_KEY, seed.teams)
  if (seed.defaultAgent !== undefined) queryClient.setQueryData(defaultAgentQuery.queryKey, { default_agent: '', default_template: seed.defaultAgent, overridden: false })
  ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({ members: seed.members })
  const frames: Frame[] = []
  render(
    <QueryClientProvider client={queryClient}>
      <Provider store={createTestStore()}>
        <ThemeProvider>
          <MemoryRouter initialEntries={['/members']}>
            <MembersPage />
            <FirstFrameProbe onFrame={(f) => frames.push(f)} />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return frames
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
})

describe('MembersPage first paint', () => {
  it('a return to the tab paints the roster already folded beside the opening mate', async () => {
    const frames = renderPage({ members: ROSTER, teams: [], defaultAgent: '' })
    expect(frames).toHaveLength(1)
    // The first frame is the final layout: the column folded, the rows drawn.
    expect(frames[0].folded).toBe(true)
    expect([...frames[0].rows].sort()).toEqual(['pilot', 'radar', 'scribe'])
    // The pane is not blank while the crewmate opens: it says so.
    expect(frames[0].waiting).toBe(true)
    // ...and the page then opens the last-chatted crewmate under that layout.
    expect(await screen.findByTestId('chat-pane-stub')).toHaveTextContent('member-scribe')
    expect(snapshot()).toEqual({ ...frames[0], waiting: false })
  })

  it('a phone paints no roster page before it lands either: the roster goes straight to the closed drawer', async () => {
    // Below md the page lands on a crewmate too, with the roster in a drawer.
    // Its first frame must not draw the roster as the page, only for the next
    // frame to move it into the drawer.
    const realMatchMedia = window.matchMedia
    window.matchMedia = vi.fn().mockImplementation((q: string) => ({
      matches: /max-width/.test(q),
      media: q,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }))
    try {
      const frames = renderPage({ members: ROSTER, teams: [], defaultAgent: '' })
      expect(frames).toHaveLength(1)
      expect(frames[0].folded).toBe(true)
      expect(frames[0].waiting).toBe(true)
      expect(await screen.findByTestId('chat-pane-stub')).toHaveTextContent('member-scribe')
      // Landed: the roster lives in the drawer, which stays closed.
      expect(screen.queryByTestId('roster-drawer')).toBeNull()
      expect(screen.queryByTestId('member-roster')).toBeNull()
    } finally {
      window.matchMedia = realMatchMedia
    }
  })

  it('an empty roster paints its open column first, and keeps it', async () => {
    const frames = renderPage({ members: [row('default', 0)], teams: [], defaultAgent: '' })
    expect(frames[0].folded).toBe(false)
    expect(frames[0].waiting).toBe(false)
    await waitFor(() => expect(screen.getAllByTestId('crewmate-empty-hero').length).toBeGreaterThan(0))
    expect(snapshot().folded).toBe(false)
    expect(screen.queryByTestId('chat-pane-stub')).toBeNull()
  })

  it('holds the rows until the team read answers, so a flat list never regroups', async () => {
    const frames = renderPage({ members: ROSTER, defaultAgent: '' })
    // Teams not answered yet: no rows rather than a flat list that would
    // reshuffle under team headers a beat later.
    expect(frames[0].rows).toEqual([])
    // ...and the count above it says unknown, not "3 crewmates" over nothing.
    expect(screen.getByTestId('member-count')).toHaveTextContent('\u2014')
    await act(async () => {
      resolveTeams({ teams: [{ id: 't1', name: 'Ops', members: ['radar'] }] })
    })
    await waitFor(() => expect(snapshot().groups).toBeGreaterThan(0))
    expect(snapshot().rows.sort()).toEqual(['pilot', 'radar', 'scribe'])
    expect(screen.getByTestId('member-count')).toHaveTextContent('3 mates')
  })
})
