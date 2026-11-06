import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import MeetingView from './MeetingView'

const state = vi.hoisted(() => ({
  addAttachment: vi.fn(),
  settled: false,
  session: {} as Record<string, unknown>,
}))

vi.mock('../../i18n/t', () => ({ i18nT: (key: string) => key }))
vi.mock('./hooks/useMeetingSession', () => ({
  useMeetingSession: () => state.session,
}))
vi.mock('./components/AgentPillBar', async importOriginal => {
  const { default: AgentPillBar } = await importOriginal<typeof import('./components/AgentPillBar')>()
  return {
    default: (props: Parameters<typeof AgentPillBar>[0]) => (
      <AgentPillBar {...props} onAddAttachment={() => {
        void Promise.resolve(props.onAddAttachment()).then(() => { state.settled = true })
      }} />
    ),
  }
})
vi.mock('./components/MeetingTitle', () => ({ default: () => null }))
vi.mock('./components/MeetingWorkspace', () => ({ default: () => null }))
vi.mock('./components/BroadcastBar', () => ({ default: () => null }))
vi.mock('./TaskReviewView', () => ({
  default: () => <div data-testid="task-review">Reviewing tasks</div>,
}))

function session() {
  return {
    meta: { title: 'Planning', attachments: [] },
    status: 'active',
    agents: [], enabledAgents: [], enabledIds: [], mutedAgents: [],
    outputs: {}, outputEdits: {}, editingOutput: false, tasks: [], transcript: [],
    partialTranscript: '', transcriptFull: false, caption: '', chatViewAgents: [],
    selectedPreset: '', loading: false, error: null, agentsPaused: false, syncing: false,
    refresh: vi.fn(), setSelectedPreset: vi.fn(), setTranslationOpen: vi.fn(),
    toggleChatView: vi.fn(), saveOutput: vi.fn(), revertOutput: vi.fn(),
    translation: {
      open: false, language: '', languageLabel: '', lines: [],
      pending: 0, dropped: 0, loading: false,
    },
    pending: { filing: false, settingStatus: null, starting: false },
    note: { open: false, draft: '', dirty: false, loaded: true },
    actions: {
      addAttachment: state.addAttachment,
      addTask: vi.fn(), archiveTask: vi.fn(), backToMeeting: vi.fn(), broadcast: vi.fn(),
      deleteTask: vi.fn(), fileTask: vi.fn(), messageAgent: vi.fn(), mute: vi.fn(),
      pause: vi.fn(), removeAttachment: vi.fn(), rename: vi.fn(), resetAgents: vi.fn(),
      resume: vi.fn(), review: vi.fn(), start: vi.fn(), stop: vi.fn(),
      toggleAgent: vi.fn(), unarchiveTask: vi.fn(), updateTask: vi.fn(),
    },
  }
}

const renderView = (eventId = 'event-1') => (
  <MeetingView eventId={eventId} config={undefined}
    onBack={() => {}} onOpenSettings={() => {}} notify={() => {}} />
)

async function openLinkPrompt(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('button', { name: 'apps.meetings.pillBar.manageAttachments' }))
  await user.click(await screen.findByRole('menuitem', { name: 'apps.meetings.pillBar.addLink' }))
  return screen.findByRole('dialog')
}

beforeEach(() => {
  state.addAttachment.mockReset()
  state.settled = false
  state.session = session()
})

describe('MeetingView attachment prompt', () => {
  it.each([
    ['review', { status: 'reviewing' }],
    ['loading', { loading: true }],
    ['error', { error: new Error('meeting unavailable') }],
  ])('cancels a pending attachment when the meeting enters %s', async (_name, changes) => {
    const user = userEvent.setup()
    const view = render(renderView())
    await openLinkPrompt(user)
    await screen.findByRole('dialog')
    state.session = { ...session(), ...changes }
    view.rerender(renderView())
    await waitFor(() => expect(state.settled).toBe(true))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(state.addAttachment).not.toHaveBeenCalled()
  })

  it('cancels a pending attachment when another meeting replaces it', async () => {
    const user = userEvent.setup()
    const view = render(renderView())
    await openLinkPrompt(user)
    await screen.findByRole('dialog')
    view.rerender(renderView('event-2'))
    await waitFor(() => expect(state.settled).toBe(true))
    expect(state.addAttachment).not.toHaveBeenCalled()
  })

  it('shows the required-link message and keeps the prompt open', async () => {
    const user = userEvent.setup()
    render(renderView())
    await openLinkPrompt(user)
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'apps.meetings.pillBar.addLink' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('apps.meetings.meeting.attachRequired')
    expect(state.addAttachment).not.toHaveBeenCalled()
    expect(state.settled).toBe(false)
  })

  it('does not add a link accepted as the meeting leaves the live view', async () => {
    const user = userEvent.setup()
    const view = render(renderView())
    await openLinkPrompt(user)
    const dialog = await screen.findByRole('dialog')
    fireEvent.change(within(dialog).getByRole('textbox'), { target: { value: 'https://example.com' } })
    act(() => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'apps.meetings.pillBar.addLink' }))
      state.session = { ...session(), status: 'reviewing' }
      view.rerender(renderView())
    })
    await waitFor(() => expect(state.settled).toBe(true))
    expect(state.addAttachment).not.toHaveBeenCalled()
  })

  it.each([
    ['  https://example.com/paper  ', 'https://example.com/paper', 'example.com'],
    ['  notes without a URL  ', 'notes without a URL', 'notes without a URL'],
  ])('adds %j labelled by hostname, or by its text when not a URL', async (typed, url, label) => {
    const user = userEvent.setup()
    render(renderView())
    await openLinkPrompt(user)
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByRole('textbox'), typed)
    await user.click(within(dialog).getByRole('button', { name: 'apps.meetings.pillBar.addLink' }))
    await waitFor(() => expect(state.addAttachment).toHaveBeenCalledWith(url, label))
  })

  it.each(['click', 'Enter'])('returns focus to the attachment trigger after adding a link with %s', async activation => {
    const user = userEvent.setup()
    render(renderView())
    const trigger = screen.getByRole('button', { name: 'apps.meetings.pillBar.manageAttachments' })
    await user.click(trigger)
    const addLink = await screen.findByRole('menuitem', { name: 'apps.meetings.pillBar.addLink' })
    await waitFor(() => expect(addLink).toHaveFocus())
    if (activation === 'click') await user.click(addLink)
    else await user.keyboard('{Enter}')
    const dialog = await screen.findByRole('dialog')
    const input = within(dialog).getByRole('textbox')
    await waitFor(() => expect(input).toHaveFocus())
    await user.type(input, 'https://example.com/paper')
    if (activation === 'click') {
      await user.click(within(dialog).getByRole('button', { name: 'apps.meetings.pillBar.addLink' }))
    } else {
      await user.keyboard('{Enter}')
    }
    await waitFor(() => expect(state.addAttachment).toHaveBeenCalledWith('https://example.com/paper', 'example.com'))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    await waitFor(() => expect(trigger).toHaveFocus())
    expect(screen.queryByRole('menu')).toBeNull()
  })

  it('adds nothing and closes the menu after Cancel', async () => {
    const user = userEvent.setup()
    render(renderView())
    await openLinkPrompt(user)
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'components.confirmDialog.cancel' }))
    await waitFor(() => expect(state.settled).toBe(true))
    expect(state.addAttachment).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
    await waitFor(() => expect(screen.getByRole('button', { name: 'apps.meetings.pillBar.manageAttachments' })).toHaveFocus())
  })
})
