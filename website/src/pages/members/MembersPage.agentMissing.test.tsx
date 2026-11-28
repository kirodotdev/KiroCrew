import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

/* A bot whose agent file is gone (its package was uninstalled, or the file was
 * deleted) stays on the roster, because it still holds its memory and chats.
 * The roster row says "Agent missing", and the bot's thread says why it cannot
 * reply and offers Delete bot, which opens the editor on its Danger zone. Same
 * api mock shape as MembersPage.copyDoor.test.tsx. */
vi.mock('../../api/client', () => ({
  api: {
    members: vi.fn(),
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
    kirocrewAgents: vi.fn(() => Promise.resolve({ agents: [], default_agent: 'kirocrew' })),
    memberProjections: vi.fn(() => Promise.resolve({ asOfSeq: 0, values: {} })),
    memberBriefing: vi.fn(() => Promise.resolve({ slug: '', member: '', supported: true, text: '', updated_ts: null, redacted: false, truncated: false })),
    crewBoard: vi.fn(() => Promise.reject(Object.assign(new Error('no_ledger'), { status: 404 }))),
  },
}))

vi.mock('../../components/ChatPane', () => ({
  default: ({ slotKey }: { slotKey: string }) => <div data-testid="chat-pane-stub">{slotKey}</div>,
}))
// The editor is a probe here: which bot it opened on, and the section it was
// told to land on. Its own delete confirm is CrewEditorNextDialog's test.
vi.mock('../../components/crew/CrewEditorModal', () => ({
  default: ({ ctl, initialSection }: { ctl: { editing: string; close: () => void }; initialSection?: string }) =>
    ctl.editing ? (
      <div data-testid="crew-editor-dialog-open" data-section={initialSection ?? ''}>
        {ctl.editing}
        <button type="button" data-testid="crew-editor-close" onClick={ctl.close}>close</button>
      </div>
    ) : null,
}))
vi.mock('../../components/crew/useCrewEditor', () => ({
  useCrewEditor: ({ editingName, onClose }: { editingName: string; onClose: () => void }) =>
    ({ open: !!editingName, editing: editingName, dirtyPanes: new Set(), close: onClose }),
  INHERIT_MODEL: 'auto',
}))

import { api } from '../../api/client'
import MembersPage from './MembersPage'

const THREAD_OPEN_TIMEOUT_MS = 5000

function row(name: string, extra: Record<string, unknown> = {}) {
  return {
    name, slug: name, slot_key: `member-${name}`, running: false, kiro_agent: name,
    workspace: 'default', memory_store: 'default', model: '', source: 'kirocrew', starred: false,
    dashboard_created: true, ...extra,
  }
}

async function openThread(member: string) {
  vi.mocked(api.members).mockResolvedValue({ members: [row('alpha'), row('beta', { agent_missing: true })] })
  renderWithProviders(<MembersPage />, { route: `/members?member=${member}` })
  await waitFor(() => expect(screen.getByTestId('chat-pane-stub').textContent).toBe(`member-${member}`), { timeout: THREAD_OPEN_TIMEOUT_MS })
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  vi.mocked(api.kirocrewAgents).mockResolvedValue({ agents: [{ ...row('beta'), description: '' }], default_agent: 'kirocrew' })
})

describe('MembersPage: a bot whose agent file is gone', () => {
  it('marks only that bot on the roster, and keeps it listed', async () => {
    await openThread('alpha')
    const marks = await screen.findAllByTestId('member-agent-missing')
    expect(marks).toHaveLength(1)
    expect(marks[0]).toHaveTextContent('Agent missing')
    expect(marks[0].closest('li')).toHaveTextContent('beta')
    // A bot whose agent is there gets no notice on its thread.
    expect(screen.queryByTestId('member-agent-missing-notice')).toBeNull()
  })

  it('says why on the thread and opens the editor on Delete bot, deleting nothing itself', async () => {
    await openThread('beta')
    const notice = await screen.findByTestId('member-agent-missing-notice')
    // It names the agent (the fixture's kiro_agent is the bot's own name).
    expect(notice).toHaveTextContent("This bot can't reply: its agent “beta” is missing.")
    // It says where to bring the agent back, and what the button and deleting do.
    expect(notice).toHaveTextContent('enable that app in Apps › Library')
    expect(notice).toHaveTextContent('put its file back')
    expect(notice).toHaveTextContent('opens the delete step')
    expect(notice).toHaveTextContent('removes the bot with its picture and goal check-ins, and keeps its memory and past chats')
    expect(screen.queryByTestId('crew-editor-dialog-open')).toBeNull()

    const remove = await screen.findByTestId('member-agent-missing-remove')
    // The ellipsis: the click opens a confirm, it does not delete.
    expect(remove).toHaveTextContent('Delete bot…')
    fireEvent.click(remove)

    const editor = await screen.findByTestId('crew-editor-dialog-open')
    expect(editor).toHaveTextContent('beta')
    expect(editor).toHaveAttribute('data-section', 'danger')
    // The row is still there: only the editor's own confirm deletes.
    expect(screen.getAllByTestId('member-agent-missing')).toHaveLength(1)
  })

  it('keeps the notice but offers no Delete bot when the registry does not list the bot', async () => {
    vi.mocked(api.kirocrewAgents).mockResolvedValue({ agents: [], default_agent: 'kirocrew' })
    await openThread('beta')
    const notice = await screen.findByTestId('member-agent-missing-notice')
    expect(notice).toHaveTextContent('is missing')
    await waitFor(() => expect(api.kirocrewAgents).toHaveBeenCalled())
    await act(async () => { await vi.mocked(api.kirocrewAgents).mock.results[0].value })
    expect(screen.queryByTestId('member-agent-missing-remove')).toBeNull()
    // With no button, the line that explains it is gone too.
    expect(notice).not.toHaveTextContent('opens the delete step')
  })

  it('still reads right when the roster row carries no agent name', async () => {
    vi.mocked(api.members).mockResolvedValue({ members: [row('alpha'), row('beta', { agent_missing: true, kiro_agent: '' })] })
    renderWithProviders(<MembersPage />, { route: '/members?member=beta' })
    expect(await screen.findByTestId('member-agent-missing-notice')).toHaveTextContent("This bot can't reply: its agent is missing.")
  })

  it('offers no Delete bot on the default bot, which cannot be deleted', async () => {
    vi.mocked(api.kirocrewAgents).mockResolvedValue({ agents: [{ ...row('beta'), description: '' }], default_agent: 'beta' })
    await openThread('beta')
    expect(await screen.findByTestId('member-agent-missing-notice')).toBeInTheDocument()
    // The button waits for the registry's answer; read its absence after it.
    await waitFor(() => expect(api.kirocrewAgents).toHaveBeenCalled())
    await act(async () => { await vi.mocked(api.kirocrewAgents).mock.results[0].value })
    expect(screen.queryByTestId('member-agent-missing-remove')).toBeNull()
  })

  it('does not land the next editor open on the Danger zone', async () => {
    await openThread('beta')
    fireEvent.click(await screen.findByTestId('member-agent-missing-remove'))
    expect(await screen.findByTestId('crew-editor-dialog-open')).toHaveAttribute('data-section', 'danger')
    fireEvent.click(screen.getByTestId('crew-editor-close'))
    await waitFor(() => expect(screen.queryByTestId('crew-editor-dialog-open')).toBeNull())

    // The ordinary way in: the identity pill's card, then Edit.
    fireEvent.click(await screen.findByTestId('member-identity-pill'))
    const card = await screen.findByTestId('crew-profile-panel')
    fireEvent.click(await within(card).findByTestId('crew-profile-edit'))

    expect(await screen.findByTestId('crew-editor-dialog-open')).toHaveAttribute('data-section', 'general')
  })
})
