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
  },
}))

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
    fireEvent.click(star)
    expect(api.updateKirocrewAgent).toHaveBeenCalledWith('pkg-a', { starred: true })
    expect(screen.getByTestId('member-star-pkg-a')).toHaveAttribute('aria-pressed', 'true')
    // Does not open the member's thread — the star is a sibling of the row.
    expect(api.memberThread).not.toHaveBeenCalled()
  })

  it('reverts the optimistic flip when the write fails', async () => {
    ;(api.updateKirocrewAgent as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('403'))
    await renderPage()
    fireEvent.click(screen.getByTestId('member-star-pkg-a'))
    await waitFor(() =>
      expect(screen.getByTestId('member-star-pkg-a')).toHaveAttribute('aria-pressed', 'false'),
    )
  })
})
