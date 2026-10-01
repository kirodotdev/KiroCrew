import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

/* Same api mock shape as MembersPage.test.tsx, plus the crew update endpoint
 * the star button writes through. */
vi.mock('../../api/client', () => ({
  api: {
    members: vi.fn(),
    // The roster's team grouping reads the team list; "no teams" keeps the
    // list flat, which is the shape every case here was written against.
    teams: { list: vi.fn(() => Promise.resolve({ teams: [] })) },
    // The page opens a member on arrival, so the thread endpoint must answer
    // from the first render; echo the slug back as the member (happy path).
    memberThread: vi.fn((slug: string) =>
      Promise.resolve({ slot_key: 'member-' + slug, slug, member: slug, created: true }),
    ),
    memberActivity: vi.fn(() => Promise.resolve({ slug: '', member: '', capped: false, entries: [] })),
    crons: vi.fn(() => Promise.resolve({ jobs: [] })),
    webhooks: vi.fn(() => Promise.resolve({ tokens: [] })),
    // The drawer's wake block reads the default crew through the shared
    // ['default-agent'] query (defaultAgentQuery), not the whole registry.
    defaultAgent: vi.fn(() => Promise.resolve({ default_agent: '' })),
    updateKirocrewAgent: vi.fn(() => Promise.resolve({ ok: true })),
    autonudgeList: vi.fn(() => Promise.resolve({ enabled: true, loops: [] })),
    // The drawer's webview section. Stubbed as "nothing published" so it renders
    // its empty state: an unstubbed reader rejects, the section shows an
    // ErrorNotice of its own, and assertions that read the LAST ErrorNotice props
    // then pick up the webview's failure instead of the one under test.
    memberPanel: vi.fn(() => Promise.resolve({ panel: null, html: null })),
  },
}))

const FAKE_REPORT = { message: 'Forbidden', endpoint: '/api/agents/pkg-a', status: 403 }
vi.mock('../../utils/errorReport', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../utils/errorReport')>()
  return { ...actual, findReport: vi.fn((m: string | null | undefined) => (m === 'Forbidden' ? FAKE_REPORT : undefined)) }
})
// Pass-through spy: renders the real component but records the props it got.
vi.mock('../../components/ErrorNotice', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../components/ErrorNotice')>()
  const Real = actual.default
  return { ...actual, default: vi.fn((props: Parameters<typeof Real>[0]) => Real(props)) }
})

vi.mock('../../components/ChatPane', () => ({
  default: ({ slotKey }: { slotKey: string }) => <div data-testid="chat-pane-stub">{slotKey}</div>,
}))

const navigateSpy = vi.fn()
vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>()
  return { ...actual, useNavigate: () => navigateSpy }
})

import { api } from '../../api/client'
import MembersPage from './MembersPage'
import { matchesSource, parseSourceFilter } from './rosterFilter'
import { findReport } from '../../utils/errorReport'
import ErrorNoticeMock from '../../components/ErrorNotice'

function row(name: string, overrides: Record<string, unknown> = {}) {
  return {
    name,
    slug: name,
    slot_key: '',
    running: false,
    kiro_agent: name,
    workspace: 'default',
    memory_store: 'default',
    model: '',
    source: 'package',
    starred: false,
    ...overrides,
  }
}

/** A roster shaped like a real host: one hand-made crew, one shipped crew, and
 *  a package-installed majority — the mix the filters exist to tame. */
const ROSTER = [
  row('conductor', { source: 'kirocrew', starred: true }),
  row('kirocrew', { source: 'builtin' }),
  row('pkg-a'),
  row('pkg-b'),
  row('legacy-aim', { source: 'aim' }),
]

async function renderPage(members = ROSTER) {
  ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({ members })
  const utils = renderWithProviders(<MembersPage />)
  await waitFor(() => expect(api.members).toHaveBeenCalled())
  // A fresh visit with nothing remembered opens no one now (#11763), so this
  // waits on the roster row itself (always rendered) rather than a thread
  // header. Scoped to the roster so the wait is unambiguous even once a
  // member is opened later in a case.
  await within(await screen.findByTestId('member-roster')).findByText(members[0].name)
  return utils
}

const names = () =>
  Array.from(document.querySelectorAll('[data-testid^="member-star-"]')).map((el) =>
    el.getAttribute('data-testid')!.replace('member-star-', ''),
  )

/** The filters live in the search row's sort/filter menu (the sidebar's
 *  idiom), so a test opens it first — Enter on the trigger, as the sidebar's
 *  own filter tests do — and the rows stay open across toggles. */
async function openFilters() {
  fireEvent.keyDown(screen.getByTestId('member-filter-menu'), { key: 'Enter' })
  await screen.findByTestId('member-filter-starred')
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
})

describe('matchesSource', () => {
  it('buckets the two known origins and treats everything else as package', () => {
    expect(matchesSource({ source: 'kirocrew' }, 'mine')).toBe(true)
    expect(matchesSource({ source: 'builtin' }, 'builtin')).toBe(true)
    expect(matchesSource({ source: 'package' }, 'package')).toBe(true)
    // Legacy spelling older configs still carry, and a missing field.
    expect(matchesSource({ source: 'aim' }, 'package')).toBe(true)
    expect(matchesSource({}, 'package')).toBe(true)
    expect(matchesSource({ source: 'kirocrew' }, 'package')).toBe(false)
    expect(matchesSource({ source: 'kirocrew' }, 'all')).toBe(true)
  })

  it('parseSourceFilter rejects junk from storage', () => {
    expect(parseSourceFilter(null)).toBe('all')
    expect(parseSourceFilter('mine')).toBe('mine')
    expect(parseSourceFilter('everything')).toBe('all')
  })
})

describe('MembersPage filters', () => {
  it('shows every member with no filter active', async () => {
    await renderPage()
    expect(names()).toEqual(['conductor', 'kirocrew', 'legacy-aim', 'pkg-a', 'pkg-b'])
  })

  it('shows per-bucket counts on the origin rows, as a separate node from the label', async () => {
    await renderPage()
    await openFilters()
    // Count is its own node, not fused to the label ("Built-in1").
    expect(within(screen.getByTestId('member-filter-source-mine')).getByText('1')).toBeInTheDocument()
    expect(within(screen.getByTestId('member-filter-source-builtin')).getByText('1')).toBeInTheDocument()
    expect(within(screen.getByTestId('member-filter-source-package')).getByText('3')).toBeInTheDocument()
    expect(screen.getByTestId('member-filter-source-builtin')).toHaveTextContent(/Built-in/)
  })

  it('the search row is the sidebar\'s shared SearchFilterBar: field, clear button, trailing menu button', async () => {
    await renderPage()
    const box = screen.getByTestId('member-search') as HTMLInputElement
    expect(screen.queryByTestId('member-search-clear')).toBeNull()
    fireEvent.change(box, { target: { value: 'pkg' } })
    expect(names()).toEqual(['pkg-a', 'pkg-b'])
    // The clear button appears with text and clears it, like the sidebar's.
    fireEvent.click(screen.getByTestId('member-search-clear'))
    expect(box.value).toBe('')
    expect(names()).toHaveLength(5)
    // The menu trigger is the sidebar's 24px filter button, docked in the field.
    const trigger = screen.getByTestId('member-filter-menu')
    expect(trigger.className).toMatch(/\bw-6\b/)
    expect(trigger.getAttribute('aria-label')).toBe('Sort and filter crewmates')
  })

  it('header reads "N of M" while a filter narrows the list, plain count otherwise', async () => {
    await renderPage()
    expect(screen.getByTestId('member-count')).toHaveTextContent('5 crewmates')
    await openFilters()
    fireEvent.click(screen.getByTestId('member-filter-starred'))
    expect(screen.getByTestId('member-count')).toHaveTextContent('1 of 5 crewmates')
    fireEvent.click(screen.getByTestId('member-filter-starred'))
    // The search box is not a "filter" for this purpose: it is transient.
    fireEvent.change(screen.getByTestId('member-search'), { target: { value: 'pkg' } })
    expect(screen.getByTestId('member-count')).toHaveTextContent('5 crewmates')
  })

  it('starred-only keeps just the starred rows and persists the toggle', async () => {
    await renderPage()
    await openFilters()
    fireEvent.click(screen.getByTestId('member-filter-starred'))
    expect(names()).toEqual(['conductor'])
    expect(screen.getByTestId('member-filter-starred')).toHaveAttribute('aria-checked', 'true')
    expect(localStorage.getItem('mc-members-starred-only')).toBe('1')
  })

  it('active filters show as ONE aggregate chip under the search row; the chip clears them all', async () => {
    await renderPage()
    // Nothing narrows the list: no chip row at all, not an empty one.
    expect(screen.queryByTestId('member-filter-chips')).toBeNull()
    await openFilters()
    fireEvent.click(screen.getByTestId('member-filter-starred'))
    fireEvent.click(screen.getByTestId('member-filter-source-mine'))
    // One control naming every active filter with its count — never one
    // button per filter (AUTOSDE max-two-buttons-per-row).
    const chips = screen.getByTestId('member-filter-chips')
    expect(chips.querySelectorAll('button')).toHaveLength(1)
    const chip = screen.getByTestId('member-filter-chip')
    // The visible text is the click's outcome, the same sentence as the aria name.
    expect(chip).toHaveTextContent('Clear Starred (1), Mine (1) filter')
    expect(chip).toHaveAttribute('aria-label', 'Clear Starred and Mine filter')
    // The search text is not a filter for this purpose: no chip change.
    fireEvent.change(screen.getByTestId('member-search'), { target: { value: 'con' } })
    expect(chips.querySelectorAll('button')).toHaveLength(1)
    // One click clears every filter and persists the clear; the row goes away.
    fireEvent.click(chip)
    expect(screen.queryByTestId('member-filter-chips')).toBeNull()
    expect(localStorage.getItem('mc-members-starred-only')).toBe('0')
    expect(localStorage.getItem('mc-members-source')).toBe('all')
  })

  it('restores a persisted starred-only filter on mount', async () => {
    localStorage.setItem('mc-members-starred-only', '1')
    await renderPage()
    expect(names()).toEqual(['conductor'])
  })

  it('origin rows filter by origin and choosing the active one clears it', async () => {
    await renderPage()
    await openFilters()
    fireEvent.click(screen.getByTestId('member-filter-source-package'))
    expect(names()).toEqual(['legacy-aim', 'pkg-a', 'pkg-b'])
    expect(localStorage.getItem('mc-members-source')).toBe('package')
    fireEvent.click(screen.getByTestId('member-filter-source-mine'))
    expect(names()).toEqual(['conductor'])
    fireEvent.click(screen.getByTestId('member-filter-source-mine'))
    expect(names()).toEqual(['conductor', 'kirocrew', 'legacy-aim', 'pkg-a', 'pkg-b'])
    expect(localStorage.getItem('mc-members-source')).toBe('all')
  })

  it('filters compose with the search box', async () => {
    await renderPage()
    await openFilters()
    fireEvent.click(screen.getByTestId('member-filter-source-package'))
    fireEvent.change(screen.getByTestId('member-search'), { target: { value: 'pkg-b' } })
    expect(names()).toEqual(['pkg-b'])
  })

  it('offers a clear action when the filters hide everyone, not the empty-roster copy', async () => {
    await renderPage()
    await openFilters()
    fireEvent.click(screen.getByTestId('member-filter-starred'))
    fireEvent.click(screen.getByTestId('member-filter-source-package'))
    expect(names()).toEqual([])
    expect(screen.getByTestId('member-filtered-out')).toBeInTheDocument()
    expect(screen.queryByText(/No crewmates yet/i)).toBeNull()
    fireEvent.click(screen.getByTestId('member-filters-clear'))
    expect(names()).toHaveLength(5)
    expect(localStorage.getItem('mc-members-starred-only')).toBe('0')
    expect(localStorage.getItem('mc-members-source')).toBe('all')
    expect(localStorage.getItem('mc-members-status')).toBe('[]')
  })

  it('status rows filter on the live state and OR together, persisting the set', async () => {
    // `running` is the roster snapshot's cold-start value; with no slot frame
    // for these members it is what isRunning reads.
    await renderPage([
      row('conductor', { source: 'kirocrew', starred: true, running: true }),
      row('kirocrew', { source: 'builtin' }),
      row('pkg-a', { running: true }),
      row('pkg-b'),
    ])
    await openFilters()
    // Counts sit right-aligned like the origin rows', 0 included — a zero-count
    // row is the one that blanks the list, so it is never hidden.
    expect(within(screen.getByTestId('member-filter-status-working')).getByText('2')).toBeInTheDocument()
    expect(within(screen.getByTestId('member-filter-status-unread')).getByText('0')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('member-filter-status-working'))
    expect(names()).toEqual(['conductor', 'pkg-a'])
    expect(screen.getByTestId('member-filter-status-working')).toHaveAttribute('aria-checked', 'true')
    expect(JSON.parse(localStorage.getItem('mc-members-status') || '[]')).toEqual(['working'])
    // A second status widens the set (OR), so a member in either state shows.
    fireEvent.click(screen.getByTestId('member-filter-status-unread'))
    expect(names()).toEqual(['conductor', 'pkg-a'])
    expect(screen.getByTestId('member-count')).toHaveTextContent('2 of 4 crewmates')
    fireEvent.click(screen.getByTestId('member-filter-status-working'))
    // Only "unread" left and nothing is unread: the filters, not the roster, emptied the list.
    expect(names()).toEqual([])
    expect(screen.getByTestId('member-filtered-out')).toBeInTheDocument()
  })

  it('sort switches between recent activity and name and persists', async () => {
    await renderPage([
      row('zed', { last_active_ts: 300 }),
      row('alpha', { last_active_ts: 100 }),
      row('mid', { last_active_ts: 200 }),
    ])
    expect(names()).toEqual(['zed', 'mid', 'alpha'])
    await openFilters()
    expect(screen.getByTestId('member-sort-recent')).toHaveAttribute('aria-checked', 'true')
    fireEvent.click(screen.getByTestId('member-sort-name'))
    expect(names()).toEqual(['alpha', 'mid', 'zed'])
    expect(localStorage.getItem('mc-members-sort')).toBe('name')
  })

  it('restores a persisted sort on mount', async () => {
    localStorage.setItem('mc-members-sort', 'name')
    await renderPage([row('zed', { last_active_ts: 300 }), row('alpha', { last_active_ts: 100 })])
    expect(names()).toEqual(['alpha', 'zed'])
  })
})

describe('MembersPage star', () => {
  it('toggling the star writes the crew record and flips the row optimistically', async () => {
    await renderPage()
    const star = screen.getByTestId('member-star-pkg-a')
    expect(star).toHaveAttribute('aria-pressed', 'false')
    // 24x24 touch target around the 13px glyph.
    expect(star.className).toMatch(/\bw-6\b/)
    expect(star.className).toMatch(/\bh-6\b/)
    fireEvent.click(star)
    // The write goes through useMutation: its onMutate first cancels any
    // in-flight roster refetch (so a stale row cannot land on the optimistic
    // one), which puts the flip and the PUT a microtask after the click.
    await waitFor(() => expect(api.updateKirocrewAgent).toHaveBeenCalledWith('pkg-a', { starred: true }))
    expect(screen.getByTestId('member-star-pkg-a')).toHaveAttribute('aria-pressed', 'true')
    // Does not open the member's thread — the star is a sibling of the row.
    // (A fresh visit opens no one now (#11763), so starring pkg-a must not be
    // the thing that posts its thread either.)
    expect(api.memberThread).not.toHaveBeenCalledWith('pkg-a')
  })

  it('disables the star while its write is pending, so rapid toggles cannot race', async () => {
    let settle: (v: unknown) => void = () => {}
    ;(api.updateKirocrewAgent as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise((res) => { settle = res }),
    )
    await renderPage()
    const star = screen.getByTestId('member-star-pkg-a')
    fireEvent.click(star)
    await waitFor(() => expect(screen.getByTestId('member-star-pkg-a')).toBeDisabled())
    // A second click while pending is a no-op: exactly one write in flight.
    fireEvent.click(screen.getByTestId('member-star-pkg-a'))
    expect(api.updateKirocrewAgent).toHaveBeenCalledTimes(1)
    settle({ ok: true })
    await waitFor(() => expect(screen.getByTestId('member-star-pkg-a')).not.toBeDisabled())
    expect(screen.getByTestId('member-star-pkg-a')).toHaveAttribute('aria-pressed', 'true')
    // No roster refetch after a 2xx: the optimistic row IS the server's state.
    expect(api.members).toHaveBeenCalledTimes(1)
  })

  it('reverts the optimistic flip AND surfaces the failure when the write fails', async () => {
    ;(api.updateKirocrewAgent as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('Forbidden'))
    await renderPage()
    expect(screen.queryByTestId('member-star-error')).toBeNull()
    fireEvent.click(screen.getByTestId('member-star-pkg-a'))
    // Not a silent revert: the user is told the preference did not save.
    // (Wait for the notice, not for aria-pressed=false — the row is false
    // BEFORE the optimistic flip too, now that the flip rides onMutate.)
    const notice = await screen.findByTestId('member-star-error')
    expect(screen.getByTestId('member-star-pkg-a')).toHaveAttribute('aria-pressed', 'false')
    // Localized copy, not the raw server text.
    expect(notice).toHaveTextContent("Could not update this crewmate's star.")
    expect(notice).not.toHaveTextContent('Forbidden')
    // The journaled report is recovered from the THROWN message (not the
    // localized one) and handed to ErrorNotice explicitly, so the agent
    // hand-off keeps endpoint / status / code / detail.
    expect(findReport).toHaveBeenCalledWith('Forbidden')
    const noticeProps = (ErrorNoticeMock as ReturnType<typeof vi.fn>).mock.calls
      .map(([props]) => props)
      .filter((props) => props.testId === 'member-star-error' && props.report)
      .at(-1)
    expect(noticeProps?.report).toEqual(FAKE_REPORT)
    expect(noticeProps?.message).toBe("Could not update this crewmate's star.")
    // A later successful toggle clears the stale notice.
    fireEvent.click(screen.getByTestId('member-star-pkg-b'))
    await waitFor(() => expect(screen.queryByTestId('member-star-error')).toBeNull())
  })
})

/* The default roster lists a row when its DM thread holds a message, or when
 * it was created on the dashboard (`dashboard_created`: source kirocrew AND a
 * member id). Everything else is hidden until the search reaches it; the
 * default crew is listed whatever its record says. Every fixture above omits
 * both fields, as an older gateway does, and those rows stay listed. */
describe('MembersPage hides unlisted crewmates', () => {
  const NO = { dashboard_created: false, has_dm_message: false }
  const MIXED = [
    row('kirocrew', { source: 'builtin', ...NO }),
    row('radar', { source: 'kirocrew', ...NO, dashboard_created: true, display_name: 'Issue Radar' }),
    row('oncall', { source: 'radar-app', ...NO, has_dm_message: true, starred: true }),
    row('legacy-aim', { source: 'aim', ...NO }),
    row('pkg-tool', { ...NO }),
  ]
  beforeEach(() => {
    ;(api.defaultAgent as ReturnType<typeof vi.fn>).mockResolvedValue({ default_agent: 'kirocrew' })
  })

  it('lists dashboard-created and chatted rows and the default crew; the count says how many are listed', async () => {
    await renderPage(MIXED)
    await waitFor(() => expect(names()).toEqual(['radar', 'kirocrew', 'oncall']))
    expect(screen.getByTestId('member-count')).toHaveTextContent('3 crewmates')
    // Not "filtered out": hidden rows are unlisted, so no chip and no notice.
    expect(screen.queryByTestId('member-filter-chips')).toBeNull()
    expect(screen.queryByTestId('member-filtered-out')).toBeNull()
  })

  it('the search reaches a hidden row, and the count grows to include it', async () => {
    await renderPage(MIXED)
    await waitFor(() => expect(names()).toEqual(['radar', 'kirocrew', 'oncall']))
    fireEvent.change(screen.getByTestId('member-search'), { target: { value: 'pkg' } })
    expect(names()).toEqual(['pkg-tool'])
    expect(screen.getByTestId('member-count')).toHaveTextContent('4 crewmates')
    fireEvent.click(screen.getByTestId('member-search-clear'))
    expect(names()).toEqual(['radar', 'kirocrew', 'oncall'])
  })

  it('filter tallies count listed rows only, and "N of M" reads M from them', async () => {
    await renderPage(MIXED)
    await waitFor(() => expect(names()).toEqual(['radar', 'kirocrew', 'oncall']))
    await openFilters()
    // Three package-bucket rows exist; only the chatted app row is listed, so
    // the tally says 1, and the built-in default crew is the one built-in row.
    expect(within(screen.getByTestId('member-filter-source-package')).getByText('1')).toBeInTheDocument()
    expect(within(screen.getByTestId('member-filter-source-builtin')).getByText('1')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('member-filter-starred'))
    expect(names()).toEqual(['oncall'])
    expect(screen.getByTestId('member-count')).toHaveTextContent('1 of 3 crewmates')
  })

  it('a failed default-crew read lists every row and says why through ErrorNotice', async () => {
    ;(api.defaultAgent as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('default lookup down'))
    await renderPage(MIXED)
    await waitFor(() => expect(screen.getByTestId('member-default-agent-error')).toHaveTextContent("Couldn't read the default crewmate, so every crewmate is shown"))
    expect([...names()].sort()).toEqual(['kirocrew', 'legacy-aim', 'oncall', 'pkg-tool', 'radar'])
  })

  it('while the default-crew read is pending, the landing fallback waits instead of overwriting a remembered default', async () => {
    // The remembered crewmate is the default crew, which is listed only by
    // name: before the read answers it reads as unlisted, and resolving the
    // fallback then would open a substitute and overwrite the memory.
    localStorage.setItem('mc-members-last-member', 'kirocrew')
    let answer: (v: { default_agent: string }) => void = () => {}
    ;(api.defaultAgent as ReturnType<typeof vi.fn>).mockReturnValue(
      new Promise((resolve) => {
        answer = resolve
      }),
    )
    ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({ members: MIXED })
    renderWithProviders(<MembersPage />)
    await waitFor(() => expect(names()).toEqual(['radar', 'oncall']))
    expect(api.memberThread).not.toHaveBeenCalled()
    expect(localStorage.getItem('mc-members-last-member')).toBe('kirocrew')
    await act(async () => {
      answer({ default_agent: 'kirocrew' })
    })
    expect(await screen.findByTestId('chat-pane-stub')).toHaveTextContent('member-kirocrew')
    expect(localStorage.getItem('mc-members-last-member')).toBe('kirocrew')
  })

  it('a team whose every crewmate is hidden keeps its header, so the team stays reachable', async () => {
    ;(api.teams.list as ReturnType<typeof vi.fn>).mockResolvedValue({
      teams: [{ id: 't-hidden', name: 'Hidden team', members: ['legacy-aim', 'pkg-tool'] }],
    })
    try {
      await renderPage(MIXED)
      await waitFor(() =>
        expect(document.querySelector('[data-testid="team-group"][data-team="t-hidden"]')).not.toBeNull(),
      )
      expect(names()).not.toContain('pkg-tool')
    } finally {
      ;(api.teams.list as ReturnType<typeof vi.fn>).mockResolvedValue({ teams: [] })
    }
  })

  it('a remembered crewmate that the rule hides is restored, listed while open, and the memory is kept', async () => {
    localStorage.setItem('mc-members-last-member', 'pkg-tool')
    ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({ members: MIXED })
    renderWithProviders(<MembersPage />)
    expect(await screen.findByTestId('chat-pane-stub')).toHaveTextContent('member-pkg-tool')
    await waitFor(() => expect(names()).toContain('pkg-tool'))
    expect(localStorage.getItem('mc-members-last-member')).toBe('pkg-tool')
  })

  it('with only the default crew listed and every other row hidden, the most recent hidden crewmate opens and is listed', async () => {
    ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({
      members: [
        row('kirocrew', { source: 'builtin', ...NO }),
        row('default', { source: 'builtin', ...NO }),
        row('pkg-tool', { ...NO, last_active_ts: 5 }),
        row('pkg-old', { ...NO, last_active_ts: 1 }),
      ],
    })
    ;(api.defaultAgent as ReturnType<typeof vi.fn>).mockResolvedValue({ default_agent: 'default' })
    renderWithProviders(<MembersPage />)
    expect(await screen.findByTestId('chat-pane-stub')).toHaveTextContent('member-pkg-tool')
    await waitFor(() => expect(names()).toContain('pkg-tool'))
    expect(names()).not.toContain('pkg-old')
    expect(screen.queryByTestId('crewmate-empty-hero')).toBeNull()
  })

  it('below md, a roster whose every row is hidden names the search instead of an empty list', async () => {
    const own = Object.getOwnPropertyDescriptor(window, 'matchMedia')
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
      ;(api.defaultAgent as ReturnType<typeof vi.fn>).mockResolvedValue({ default_agent: '' })
      ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({
        members: [row('pkg-tool', { ...NO }), row('legacy-aim', { source: 'aim', ...NO })],
      })
      renderWithProviders(<MembersPage />)
      expect(await screen.findByTestId('member-all-hidden')).toBeTruthy()
      expect(names()).toEqual([])
      fireEvent.change(screen.getByTestId('member-search'), { target: { value: 'pkg' } })
      expect(screen.queryByTestId('member-all-hidden')).toBeNull()
      expect(names()).toEqual(['pkg-tool'])
    } finally {
      if (own) Object.defineProperty(window, 'matchMedia', own)
      else delete (window as unknown as { matchMedia?: typeof window.matchMedia }).matchMedia
    }
  })

  it('the default-crew failure notice hands the agent the journal report, not just its localized copy', async () => {
    ;(api.defaultAgent as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('Forbidden'))
    await renderPage(MIXED)
    await screen.findByTestId('member-default-agent-error')
    const noticeProps = (ErrorNoticeMock as ReturnType<typeof vi.fn>).mock.calls
      .map(([props]) => props)
      .filter((props) => props.testId === 'member-default-agent-error')
      .at(-1)
    expect(noticeProps?.report).toEqual(FAKE_REPORT)
  })

  it('a failed REFRESH turns the hide rule off too, even while an older default is still cached', async () => {
    const { queryClient } = await renderPage(MIXED)
    await waitFor(() => expect(names()).toEqual(['radar', 'kirocrew', 'oncall']))
    ;(api.defaultAgent as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('refresh failed'))
    await act(async () => {
      await queryClient.refetchQueries({ queryKey: ['default-agent'] })
    })
    await waitFor(() => expect(screen.getByTestId('member-default-agent-error')).toHaveTextContent("Couldn't read the default crewmate, so every crewmate is shown"))
    expect([...names()].sort()).toEqual(['kirocrew', 'legacy-aim', 'oncall', 'pkg-tool', 'radar'])
  })
})
