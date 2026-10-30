import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import MeetingView from '../apps/meetings/MeetingView'

const testState = vi.hoisted(() => ({
  addAttachment: vi.fn(),
  callerSettled: false,
  session: {} as Record<string, unknown>,
}))

vi.mock('../i18n/t', () => ({ i18nT: (key: string) => key }))
vi.mock('../apps/meetings/hooks/useMeetingSession', () => ({
  useMeetingSession: () => testState.session,
}))
vi.mock('../apps/meetings/components/AgentPillBar', () => ({
  default: ({ onAddAttachment }: { onAddAttachment: () => void }) => (
    <button
      onClick={() => {
        void Promise.resolve(onAddAttachment()).then(() => {
          testState.callerSettled = true
        })
      }}
    >
      Attach Link
    </button>
  ),
}))
vi.mock('../apps/meetings/components/MeetingTitle', () => ({ default: () => null }))
vi.mock('../apps/meetings/components/MeetingWorkspace', () => ({ default: () => null }))
vi.mock('../apps/meetings/components/BroadcastBar', () => ({ default: () => null }))
vi.mock('../apps/meetings/TaskReviewView', () => ({
  default: () => <div data-testid="task-review">Reviewing tasks</div>,
}))

function session(status: 'active' | 'reviewing') {
  return {
    meta: { title: 'Planning', attachments: [] },
    status,
    agents: [],
    enabledAgents: [],
    enabledIds: [],
    mutedAgents: [],
    outputs: {},
    outputEdits: {},
    editingOutput: false,
    tasks: [],
    transcript: [],
    partialTranscript: '',
    transcriptFull: false,
    caption: '',
    chatViewAgents: [],
    selectedPreset: '',
    loading: false,
    error: null,
    agentsPaused: false,
    syncing: false,
    refresh: vi.fn(),
    setSelectedPreset: vi.fn(),
    setTranslationOpen: vi.fn(),
    toggleChatView: vi.fn(),
    saveOutput: vi.fn(),
    revertOutput: vi.fn(),
    translation: {
      open: false,
      language: '',
      languageLabel: '',
      lines: [],
      pending: 0,
      dropped: 0,
      loading: false,
    },
    pending: { filing: false, settingStatus: null, starting: false },
    note: { open: false, draft: '', dirty: false, loaded: true },
    actions: {
      addAttachment: testState.addAttachment,
      addTask: vi.fn(),
      archiveTask: vi.fn(),
      backToMeeting: vi.fn(),
      broadcast: vi.fn(),
      deleteTask: vi.fn(),
      fileTask: vi.fn(),
      messageAgent: vi.fn(),
      mute: vi.fn(),
      pause: vi.fn(),
      removeAttachment: vi.fn(),
      rename: vi.fn(),
      resetAgents: vi.fn(),
      resume: vi.fn(),
      review: vi.fn(),
      start: vi.fn(),
      stop: vi.fn(),
      toggleAgent: vi.fn(),
      unarchiveTask: vi.fn(),
      updateTask: vi.fn(),
    },
  }
}

const renderView = () => (
  <MeetingView
    eventId="event-1"
    config={undefined}
    onBack={() => {}}
    onOpenSettings={() => {}}
    notify={() => {}}
  />
)

describe('MeetingView attachment prompt', () => {
  it('settles a pending Attach Link request without adding an attachment when review starts', async () => {
    testState.addAttachment.mockReset()
    testState.callerSettled = false
    testState.session = session('active')
    const user = userEvent.setup()
    const view = render(renderView())

    await user.click(screen.getByRole('button', { name: 'Attach Link' }))
    expect(await screen.findByRole('dialog')).toBeTruthy()

    testState.session = session('reviewing')
    view.rerender(renderView())

    expect(screen.getByTestId('task-review')).toBeTruthy()
    expect(screen.queryByRole('dialog')).toBeNull()
    await waitFor(() => expect(testState.callerSettled).toBe(true))
    expect(testState.addAttachment).not.toHaveBeenCalled()
  })

  it('names the missing link when Add link is pressed on a blank field', async () => {
    testState.addAttachment.mockReset()
    testState.session = session('active')
    const user = userEvent.setup()
    render(renderView())

    await user.click(screen.getByRole('button', { name: 'Attach Link' }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'apps.meetings.pillBar.addLink' }))

    expect(await within(dialog).findByRole('alert')).toHaveTextContent('apps.meetings.meeting.attachRequired')
    expect(testState.addAttachment).not.toHaveBeenCalled()
  })
})
