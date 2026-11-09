// Every exit of the meeting view asks before it drops text the user typed and
// never sent (#30636). The chat line to one agent, the broadcast bar and the
// quick-add field are one-line inputs that unmount with the view, as does an
// unsaved minutes draft, so Back, Settings, the transcription notice's Voice
// settings action and opening the meeting a refused Start names would otherwise
// lose that text without a word.

import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

import MeetingView from './MeetingView'
import { useMeetingSession } from './hooks/useMeetingSession'
import type { AgentDef } from './api'
import { settingsPath } from '../../components/settingsPath'
import EN_CATALOG from '../../i18n/locales/en.json'
import EN_MANUAL from '../../i18n/locales/en.manual.json'

const { navigate } = vi.hoisted(() => ({ navigate: vi.fn() }))
vi.mock('react-router-dom', async importOriginal => ({
  ...(await importOriginal<typeof import('react-router-dom')>()),
  useNavigate: () => navigate,
}))
vi.mock('./hooks/useMeetingSession', () => ({ useMeetingSession: vi.fn() }))

const M = EN_CATALOG.apps.meetings
const CHAT: AgentDef = { id: 'helper', name: 'Helper', widget_type: 'chat' }
const NOTES: AgentDef = { id: 'note-taker', name: 'Note Taker', widget_type: 'markdown' }
const BLOCKER = { id: 'launch-retro', title: 'Launch retrospective', reviewing: false }

function stubSession() {
  return {
    meta: { title: 'Planning session', attachments: [], started_at: '', created_at: '' },
    status: 'active',
    agents: [CHAT, NOTES],
    enabledAgents: [CHAT, NOTES],
    enabledIds: [CHAT.id, NOTES.id],
    mutedAgents: [],
    outputs: { [NOTES.id]: '# Agenda' },
    outputEdits: {},
    editingOutput: false,
    saveOutput: vi.fn(async () => undefined),
    revertOutput: vi.fn(),
    tasks: [],
    transcript: [],
    partialTranscript: '',
    transcriptFull: false,
    caption: '',
    chatViewAgents: [],
    selectedPreset: '',
    startError: new Error('refused'),
    startErrorMessage: '“Launch retrospective” is still live',
    startBlockingMeeting: BLOCKER,
    transcriptionErrorMessage: 'Streaming transcription is off',
    transcriptionErrorCode: 'stt_streaming_disabled',
    dismissStartError: vi.fn(),
    dismissTranscriptionError: vi.fn(),
    loading: false,
    error: null,
    stopError: null,
    agentsPaused: false,
    syncing: false,
    pending: { starting: false, stopping: false, settingStatus: null, filing: null, filingIds: [] },
    translation: {
      language: '', languageLabel: '', open: false, lines: [], pending: 0, dropped: 0, loading: false,
    },
    note: {
      open: false, draft: '', dirty: false, loaded: true, updatedAt: '', path: '', saving: false,
      saveFailed: false, loadFailed: false, uploading: false, uploadError: null,
    },
    leaveNote: vi.fn(async () => true),
    setNoteOpen: vi.fn(),
    setTranslationOpen: vi.fn(),
    setSelectedPreset: vi.fn(),
    toggleChatView: vi.fn(),
    refresh: vi.fn(),
    changeNote: vi.fn(),
    flushNote: vi.fn(),
    pasteNoteImage: vi.fn(),
    actions: Object.fromEntries(
      [
        'start', 'pause', 'resume', 'review', 'backToMeeting', 'stop', 'rename', 'mute',
        'toggleAgent', 'broadcast', 'messageAgent', 'resetAgents', 'addAttachment',
        'removeAttachment', 'addTask', 'updateTask', 'deleteTask', 'fileTask', 'archiveTask',
        'unarchiveTask',
      ].map(name => [name, vi.fn()]),
    ),
  }
}

function mount(over: Partial<ReturnType<typeof stubSession>> = {}) {
  vi.mocked(useMeetingSession).mockReturnValue(
    { ...stubSession(), ...over } as unknown as ReturnType<typeof useMeetingSession>,
  )
  const props = { onBack: vi.fn(), onOpenSettings: vi.fn(), onOpenMeeting: vi.fn() }
  render(
    <MemoryRouter>
      <MeetingView eventId="planning-session" config={undefined} notify={vi.fn()} {...props} />
    </MemoryRouter>,
  )
  return props
}

function openTaskSidebar() {
  const trigger = screen.getByRole('button', { name: M.meeting.moreActions })
  fireEvent.pointerDown(trigger, { pointerType: 'mouse', button: 0, ctrlKey: false })
  fireEvent.click(screen.getByRole('menuitem', { name: new RegExp(M.meeting.toggleTasks) }))
}

const typeInto = (label: string, text: string) =>
  fireEvent.change(screen.getByRole('textbox', { name: label }), { target: { value: text } })

function openMinutesEditor() {
  fireEvent.click(screen.getByRole('button', { name: M.agentPanel.edit }))
}

// Every exit dialog names the exit and what is lost, so a minutes draft alone
// says that confirming leaves the meeting too.
const UNSENT = {
  title: M.meeting.leaveUnsent,
  hint: M.meeting.leaveUnsentHint,
  confirm: M.meeting.leaveUnsentConfirm,
}
const INPUTS = [
  {
    name: 'an agent chat line',
    label: M.agentPanel.messagePlaceholder.replace('{{name}}', CHAT.name),
    ...UNSENT,
  },
  { name: 'the broadcast bar', label: M.broadcastBar.placeholder, ...UNSENT },
  { name: 'quick add', label: M.taskSidebar.quickAddPlaceholder, setup: openTaskSidebar, ...UNSENT },
  // Not a one-line input, but the other text the view drops on the way out.
  {
    name: 'a minutes draft',
    label: M.agentPanel.editorLabel.replace('{{name}}', NOTES.name),
    setup: openMinutesEditor,
    title: M.meeting.leaveWithEdits,
    hint: M.meeting.leaveWithEditsHint,
    confirm: M.meeting.leaveUnsentConfirm,
  },
]

const back = () => fireEvent.click(screen.getByRole('button', { name: M.meeting.back }))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('MeetingView exits with unsent text', () => {
  it.each(INPUTS)('asks before Back drops unsent text in $name, and leaves once discarded', async input => {
    const { onBack } = mount()
    input.setup?.()
    typeInto(input.label, 'ship it on Friday')
    back()

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(input.title)).toBeTruthy()
    expect(within(dialog).getByText(input.hint)).toBeTruthy()
    expect(onBack).not.toHaveBeenCalled()

    fireEvent.click(within(dialog).getByRole('button', { name: input.confirm }))
    await waitFor(() => expect(onBack).toHaveBeenCalledTimes(1))
  })

  it('names both losses when unsent text and a minutes draft are open', async () => {
    mount()
    openMinutesEditor()
    typeInto(M.agentPanel.editorLabel.replace('{{name}}', NOTES.name), 'ship it on Friday')
    typeInto(M.broadcastBar.placeholder, 'ship it on Friday')
    back()

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(M.meeting.leaveUnsent)).toBeTruthy()
    expect(within(dialog).getByText(M.meeting.leaveUnsentWithEditsHint)).toBeTruthy()
  })

  it.each([
    {
      exit: 'Settings',
      press: () => fireEvent.click(screen.getByRole('button', { name: M.pillBar.manageAgents })),
      left: (p: ReturnType<typeof mount>) => p.onOpenSettings,
      args: [],
    },
    {
      exit: 'Voice settings',
      press: () => fireEvent.click(screen.getByRole('button', { name: M.session.openVoiceSettings })),
      left: () => navigate,
      args: [settingsPath({ tab: 'voice' })],
    },
    {
      exit: 'opening the blocking meeting',
      press: () => fireEvent.click(screen.getByRole('button', { name: M.session.openBlockingMeeting })),
      left: (p: ReturnType<typeof mount>) => p.onOpenMeeting,
      args: [BLOCKER.id, BLOCKER.title],
    },
  ])('asks on $exit as well', async ({ press, left, args }) => {
    const props = mount()
    typeInto(M.broadcastBar.placeholder, 'ship it on Friday')
    press()

    const dialog = await screen.findByRole('dialog')
    expect(left(props)).not.toHaveBeenCalled()

    fireEvent.click(within(dialog).getByRole('button', { name: M.meeting.leaveUnsentConfirm }))
    await waitFor(() => expect(left(props)).toHaveBeenCalledWith(...args))
  })

  it('stays in the meeting, text intact, when the user cancels', async () => {
    const { onBack } = mount()
    typeInto(M.broadcastBar.placeholder, 'ship it on Friday')
    back()

    const dialog = await screen.findByRole('dialog')
    // Settled inside act: the dialog's answer resolves the exit's await.
    await act(async () => {
      fireEvent.click(
        within(dialog).getByRole('button', { name: EN_MANUAL.components.confirmDialog.cancel }),
      )
    })
    expect(onBack).not.toHaveBeenCalled()
    expect(
      (screen.getByRole('textbox', { name: M.broadcastBar.placeholder }) as HTMLInputElement).value,
    ).toBe('ship it on Friday')
  })

  it('asks about text typed while the note was still saving', async () => {
    let noteSaved: (ok: boolean) => void = () => {}
    const { onBack } = mount({
      leaveNote: vi.fn(() => new Promise<boolean>(resolve => { noteSaved = resolve })),
    })
    back()
    typeInto(M.broadcastBar.placeholder, 'ship it on Friday')
    await act(async () => { noteSaved(true) })

    expect(await screen.findByRole('dialog')).toBeTruthy()
    expect(onBack).not.toHaveBeenCalled()
  })

  it('ignores the marker on agent output, which has no value to read', async () => {
    // Agent output renders sanitized markdown under the same root, and the
    // sanitizer keeps data-* attributes, so the marker can land on a div.
    const { onBack } = mount({ outputs: { [NOTES.id]: '<div data-unsent>Agenda</div>' } })
    expect(document.querySelector('div[data-unsent]')).toBeTruthy()
    back()

    await waitFor(() => expect(onBack).toHaveBeenCalledTimes(1))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('leaves without asking when nothing sendable is typed', async () => {
    // Blank text is what Send refuses, so there is nothing to lose.
    const { onBack } = mount()
    typeInto(M.broadcastBar.placeholder, '   ')
    back()

    await waitFor(() => expect(onBack).toHaveBeenCalledTimes(1))
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})
