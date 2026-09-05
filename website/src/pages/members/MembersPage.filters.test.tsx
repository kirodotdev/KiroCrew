import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

/* Same api mock shape as MembersPage.test.tsx, plus the crew update endpoint
 * the star button writes through. */
vi.mock('../../api/client', () => ({
  api: {
    members: vi.fn(),
    memberThread: vi.fn(),
    memberActivity: vi.fn(() => Promise.resolve({ slug: '', member: '', capped: false, entries: [] })),
    crons: vi.fn(() => Promise.resolve({ jobs: [] })),
    webhooks: vi.fn(() => Promise.resolve({ tokens: [] })),
    kirocrewAgents: vi.fn(() => Promise.resolve({ agents: [], default_agent: '' })),
    updateKirocrewAgent: vi.fn(() => Promise.resolve({ ok: true })),
    autonudgeList: vi.fn(() => Promise.resolve({ enabled: true, loops: [] })),
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
import MembersPage, { matchesSource, parseSourceFilter } from './MembersPage'
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
  await screen.findByText('conductor')
  return utils
}

const names = () =>
  Array.from(document.querySelectorAll('[data-testid^="member-star-"]')).map((el) =>
    el.getAttribute('data-testid')!.replace('member-star-', ''),
  )

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

  it('shows per-bucket counts on the origin chips', async () => {
    await renderPage()
    // Count is a separate node with a gap, not fused to the label ("Built-in2").
    expect(screen.getByTestId('member-filter-source-builtin').className).toMatch(/\bgap-1\b/)
    expect(screen.getByTestId('member-filter-source-mine')).toHaveTextContent('1')
    expect(screen.getByTestId('member-filter-source-builtin')).toHaveTextContent('1')
    expect(screen.getByTestId('member-filter-source-package')).toHaveTextContent('3')
  })

  it('header reads "N of M" while a filter narrows the list, plain count otherwise', async () => {
    await renderPage()
    expect(screen.getByTestId('member-count')).toHaveTextContent('5 members')
    fireEvent.click(screen.getByTestId('member-filter-starred'))
    expect(screen.getByTestId('member-count')).toHaveTextContent('1 of 5 members')
    fireEvent.click(screen.getByTestId('member-filter-starred'))
    // The search box is not a "filter" for this purpose: it is transient.
    fireEvent.change(screen.getByTestId('member-search'), { target: { value: 'pkg' } })
    expect(screen.getByTestId('member-count')).toHaveTextContent('5 members')
  })

  it('starred-only keeps just the starred rows and persists the toggle', async () => {
    await renderPage()
    fireEvent.click(screen.getByTestId('member-filter-starred'))
    expect(names()).toEqual(['conductor'])
    expect(screen.getByTestId('member-filter-starred')).toHaveAttribute('aria-pressed', 'true')
    expect(localStorage.getItem('mc-members-starred-only')).toBe('1')
  })

  it('restores a persisted starred-only filter on mount', async () => {
    localStorage.setItem('mc-members-starred-only', '1')
    await renderPage()
    expect(names()).toEqual(['conductor'])
  })

  it('source chips filter by origin and clicking the active chip clears it', async () => {
    await renderPage()
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
    fireEvent.click(screen.getByTestId('member-filter-source-package'))
    fireEvent.change(screen.getByTestId('member-search'), { target: { value: 'pkg-b' } })
    expect(names()).toEqual(['pkg-b'])
  })

  it('offers a clear action when the filters hide everyone, not the empty-roster copy', async () => {
    await renderPage()
    fireEvent.click(screen.getByTestId('member-filter-starred'))
    fireEvent.click(screen.getByTestId('member-filter-source-package'))
    expect(names()).toEqual([])
    expect(screen.getByTestId('member-filtered-out')).toBeInTheDocument()
    expect(screen.queryByText(/No crew members yet/i)).toBeNull()
    fireEvent.click(screen.getByTestId('member-filters-clear'))
    expect(names()).toHaveLength(5)
    expect(localStorage.getItem('mc-members-starred-only')).toBe('0')
    expect(localStorage.getItem('mc-members-source')).toBe('all')
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
    expect(api.updateKirocrewAgent).toHaveBeenCalledWith('pkg-a', { starred: true })
    expect(screen.getByTestId('member-star-pkg-a')).toHaveAttribute('aria-pressed', 'true')
    // Does not open the member's thread — the star is a sibling of the row.
    expect(api.memberThread).not.toHaveBeenCalled()
  })

  it('disables the star while its write is pending, so rapid toggles cannot race', async () => {
    let settle: (v: unknown) => void = () => {}
    ;(api.updateKirocrewAgent as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise((res) => { settle = res }),
    )
    await renderPage()
    const star = screen.getByTestId('member-star-pkg-a')
    fireEvent.click(star)
    expect(screen.getByTestId('member-star-pkg-a')).toBeDisabled()
    // A second click while pending is a no-op: exactly one write in flight.
    fireEvent.click(screen.getByTestId('member-star-pkg-a'))
    expect(api.updateKirocrewAgent).toHaveBeenCalledTimes(1)
    settle({ ok: true })
    await waitFor(() => expect(screen.getByTestId('member-star-pkg-a')).not.toBeDisabled())
    expect(screen.getByTestId('member-star-pkg-a')).toHaveAttribute('aria-pressed', 'true')
  })

  it('reverts the optimistic flip AND surfaces the failure when the write fails', async () => {
    ;(api.updateKirocrewAgent as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('Forbidden'))
    await renderPage()
    expect(screen.queryByTestId('member-star-error')).toBeNull()
    fireEvent.click(screen.getByTestId('member-star-pkg-a'))
    await waitFor(() =>
      expect(screen.getByTestId('member-star-pkg-a')).toHaveAttribute('aria-pressed', 'false'),
    )
    // Not a silent revert: the user is told the preference did not save.
    const notice = screen.getByTestId('member-star-error')
    // Localized copy, not the raw server text.
    expect(notice).toHaveTextContent("Could not update this member's star.")
    expect(notice).not.toHaveTextContent('Forbidden')
    // The journaled report is recovered from the THROWN message (not the
    // localized one) and handed to ErrorNotice explicitly, so the agent
    // hand-off keeps endpoint / status / code / detail.
    expect(findReport).toHaveBeenCalledWith('Forbidden')
    const noticeProps = (ErrorNoticeMock as ReturnType<typeof vi.fn>).mock.calls.at(-1)?.[0]
    expect(noticeProps?.report).toEqual(FAKE_REPORT)
    expect(noticeProps?.message).toBe("Could not update this member's star.")
    // A later successful toggle clears the stale notice.
    fireEvent.click(screen.getByTestId('member-star-pkg-b'))
    await waitFor(() => expect(screen.queryByTestId('member-star-error')).toBeNull())
  })
})
