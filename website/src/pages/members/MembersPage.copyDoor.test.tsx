import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

/* Make a copy on a bot's Profile card lands through the import card, so the
 * page offers it exactly where it offers Import from file: to the owner, never
 * to a teammate or while the owner check failed. Same api mock shape as
 * MembersPage.freshStart.test.tsx. */
vi.mock('../../api/client', () => ({
  api: {
    members: vi.fn(),
    // The owner check behind Import from file (`useViewerIsNotOwner`): the owner.
    viewerOwnerOk: vi.fn(() => Promise.resolve(true)),
    teams: { list: vi.fn(() => Promise.resolve({ teams: [] })) },
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
    // The Profile card's own reads (the narrow-screen entry lives there).
    kirocrewAgents: vi.fn(() => Promise.resolve({ agents: [], default_agent: 'kirocrew' })),
    memberProjections: vi.fn(() => Promise.resolve({ asOfSeq: 0, values: {} })),
    memberBriefing: vi.fn(() => Promise.resolve({ slug: '', member: '', supported: true, text: '', updated_ts: null, redacted: false, truncated: false })),
    crewBoard: vi.fn(() => Promise.reject(Object.assign(new Error('no_ledger'), { status: 404 }))),
  },
}))

vi.mock('../../components/ChatPane', () => ({
  default: ({ slotKey, foldBefore }: { slotKey: string; foldBefore?: string }) => (
    <div data-testid="chat-pane-stub" data-fold-before={foldBefore ?? ''}>{slotKey}</div>
  ),
}))

import { api } from '../../api/client'
import MembersPage from './MembersPage'

const THREAD_OPEN_TIMEOUT_MS = 5000

function row(name: string) {
  return {
    name, slug: name, slot_key: `member-${name}`, running: false, kiro_agent: name,
    workspace: 'default', memory_store: 'default', model: '', source: 'kirocrew', starred: false,
  }
}

/** Open alpha's Profile card on its Profile tab, once the owner check has answered. */
async function openProfileTab() {
  ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({ members: [row('alpha')] })
  renderWithProviders(<MembersPage />, { route: '/members?member=alpha' })
  await waitFor(() => expect(screen.getByTestId('chat-pane-stub').textContent).toBe('member-alpha'), { timeout: THREAD_OPEN_TIMEOUT_MS })
  await waitFor(() => expect(api.viewerOwnerOk).toHaveBeenCalled())
  fireEvent.click(await screen.findByTestId('member-identity-pill'))
  const card = await screen.findByTestId('crew-profile-panel')
  fireEvent.click(within(card).getByRole('tab', { name: 'Profile' }))
  return within(card).findByTestId('crew-profile-export')
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  vi.mocked(api.viewerOwnerOk).mockReset()
})

describe('MembersPage — Make a copy door', () => {
  it('offers Make a copy to the owner, under Export as file', async () => {
    vi.mocked(api.viewerOwnerOk).mockResolvedValue(true)
    const exportRow = await openProfileTab()
    const copyRow = await screen.findByTestId('crew-profile-copy')
    expect(exportRow.nextElementSibling).toBe(copyRow)
  })

  it('takes Make a copy away once the owner check says a teammate, as it hides Import from file', async () => {
    // Shown while the check is out, like Import from file; gone on its answer.
    let answer: (owner: boolean) => void = () => {}
    vi.mocked(api.viewerOwnerOk).mockReturnValue(new Promise<boolean>(resolve => { answer = resolve }))
    await openProfileTab()
    expect(await screen.findByTestId('crew-profile-copy')).toBeInTheDocument()
    answer(false)
    await waitFor(() => expect(screen.queryByTestId('crew-profile-copy')).toBeNull())
    expect(screen.getByTestId('crew-profile-export')).toBeInTheDocument()
  })

  it('takes Make a copy away once the owner check fails', async () => {
    let fail: (e: Error) => void = () => {}
    vi.mocked(api.viewerOwnerOk).mockReturnValue(new Promise<boolean>((_resolve, reject) => { fail = reject }))
    await openProfileTab()
    expect(await screen.findByTestId('crew-profile-copy')).toBeInTheDocument()
    fail(Object.assign(new Error('unavailable'), { status: 503 }))
    await screen.findByTestId('member-thread-owner-check-error', undefined, { timeout: THREAD_OPEN_TIMEOUT_MS })
    expect(screen.queryByTestId('crew-profile-copy')).toBeNull()
  })
})
