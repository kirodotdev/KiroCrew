import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, act, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import { __resetPanelTabs } from '../../hooks/usePanelTabs'
import { markSlotUnread } from '../../store/dashboardSlice'

/* The unread column that hangs under the thread header's switcher chip
 * (`MembersPageThreadHeader`): one pill per OTHER crewmate with an unread
 * message, and a "+N" pill once there are more than fit. Picking a pill opens
 * that crewmate through the page's own switch path; "+N" opens the switcher's
 * list instead of choosing for the reader. */

vi.mock('../../api/client', () => ({
  api: {
    members: vi.fn(),
    teams: { list: vi.fn(() => Promise.resolve({ teams: [] })) },
    crewBoard: vi.fn(() => Promise.reject(Object.assign(new Error('no_ledger'), { status: 404 }))),
    memberRecap: vi.fn(() => Promise.reject(new Error('no recap in this test'))),
    memberThread: vi.fn(),
    memberActivity: vi.fn(() => Promise.resolve({ slug: '', member: '', capped: false, entries: [] })),
    memberBriefing: vi.fn(() => Promise.resolve({ slug: '', member: '', supported: true, text: '', updated_ts: null, redacted: false, truncated: false })),
    sessionCrewLogProjections: vi.fn(() => Promise.resolve({ folds: {}, resolved: true, writesDrained: true })),
    autonudgeList: vi.fn(() => Promise.resolve({ enabled: true, loops: [] })),
  },
}))

vi.mock('../chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../chat/FilesHomePanel', () => ({ default: () => null }))
vi.mock('../chat/FolderPanel', () => ({ default: () => null }))
vi.mock('../../components/DiffPanel', () => ({ default: () => null }))
vi.mock('../../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../../components/ArtifactPanel', () => ({ default: () => null }))
vi.mock('../../components/WebPreviewPanel', () => ({ default: () => null }))
vi.mock('../../components/McpAppFrame', () => ({ default: () => null }))
vi.mock('../../components/CliPanel', () => ({
  default: () => null,
  disposeTerminalSession: vi.fn(),
  useDeleteTerminalSession: () => ({ mutate: vi.fn() }),
}))
vi.mock('../../utils/terminalRegistry', () => ({
  useTerminalEnabled: () => true,
  useTerminalTitle: () => 'Terminal',
}))
vi.mock('../../hooks/useDevMode', () => ({ useDevMode: () => false }))
vi.mock('../../components/ChatPane', () => ({
  default: ({ slotKey }: { slotKey: string }) => <div data-testid="chat-pane-stub">{slotKey}</div>,
}))

import { api } from '../../api/client'
import MembersPage from './MembersPage'

function row(name: string) {
  return {
    name, slug: name, bound: true, slot_key: `member-${name}`, running: false,
    kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', model: '',
  }
}

/** A thread open is a chain: the roster read resolves, the URL sync picks the
 *  crewmate, the thread-open POST (`api.memberThread`) resolves, and only then
 *  does the pane mount with the confirmed slot key. A loaded shard can take
 *  longer than the 1 s default to walk it, so the waits on it say how long. */
const THREAD_OPEN_TIMEOUT_MS = 5000

/** Opens `oncall` with the other crewmates on the roster beside it. */
async function openOncall(others: string[]) {
  ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({ members: [row('oncall'), ...others.map(row)], default_agent: 'kirocrew' })
  ;(api.memberThread as ReturnType<typeof vi.fn>).mockImplementation((slug: string) =>
    Promise.resolve({ slot_key: `member-${slug}`, slug, member: slug, created: true }))
  const utils = renderWithProviders(<MembersPage />, { route: '/members?member=oncall' })
  // Roster read -> URL sync -> thread-open POST -> pane mount (THREAD_OPEN_TIMEOUT_MS).
  await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-oncall'), { timeout: THREAD_OPEN_TIMEOUT_MS })
  return utils
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  __resetPanelTabs()
  Object.defineProperty(window, 'innerWidth', { value: 1440, configurable: true, writable: true })
})

describe('MembersPage thread header unread column', () => {
  it('opens the crewmate whose unread pill is picked', async () => {
    const { store } = await openOncall(['research'])
    act(() => { store.dispatch(markSlotUnread('member-research')) })
    const column = screen.getByTestId('crewmate-unread-column-slot')
    const pill = await within(column).findByTestId('crewmate-switcher-unread-pill')
    fireEvent.click(pill)
    // The pick runs the same chain for `research`: switch -> thread-open POST -> pane mount.
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-research'), { timeout: THREAD_OPEN_TIMEOUT_MS })
    expect(api.memberThread).toHaveBeenLastCalledWith('research')
  })

  it('"+N" opens the switcher list rather than a crewmate', async () => {
    const others = ['alpha', 'bravo', 'charlie', 'delta', 'echo', 'foxtrot']
    const { store } = await openOncall(others)
    act(() => { for (const n of others) store.dispatch(markSlotUnread(`member-${n}`)) })
    const column = screen.getByTestId('crewmate-unread-column-slot')
    const more = await within(column).findByTestId('crewmate-switcher-unread-more')
    expect(screen.queryByTestId('crewmate-switcher-list')).toBeNull()
    fireEvent.click(more)
    expect(await screen.findByTestId('crewmate-switcher-list')).toBeInTheDocument()
    expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-oncall')
  })
})
