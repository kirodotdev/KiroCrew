// Behaviour coverage for the meeting session hook itself.
//
// `MeetingsSessionLogic.test.ts` already covers the exported pure decision
// functions (dedup, preset resolution, transitions) and asserts several effects
// against the SOURCE, because driving the real transcription path needs a
// WebSocket + AudioContext + MediaStream harness. This file drives the hook for
// real instead, mocking exactly two seams: the api client and the transcription
// hook. Everything between them — the three queries, the roster derivation, the
// status/microphone binding, and all seventeen mutations — is the shipped code.

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { i18nT } from '../i18n/t'

const apiMocks = vi.hoisted(() => ({
  init: vi.fn(),
  meeting: vi.fn(),
  outputs: vi.fn(),
  start: vi.fn(),
  setStatus: vi.fn(),
  stop: vi.fn(),
  mute: vi.fn(),
  toggleAgent: vi.fn(),
  dispatch: vi.fn(),
  message: vi.fn(),
  resetAgents: vi.fn(),
  attachments: vi.fn(),
  addTask: vi.fn(),
  updateTask: vi.fn(),
  deleteTask: vi.fn(),
  fileTask: vi.fn(),
  reviewTask: vi.fn(),
}))

/**
 * Stand-in for the transcription hook.
 *
 * `active` is a plain field rather than state so a test can flip it and
 * `rerender()` to reproduce the socket dropping under an active meeting — the
 * case the binding effect exists for.
 */
const stt = vi.hoisted(() => ({
  active: false,
  start: vi.fn(() => Promise.resolve()),
  stop: vi.fn(),
  onCaption: undefined as ((text: string) => void) | undefined,
  onFinal: undefined as ((text: string) => string | boolean | void) | undefined,
  onError: undefined as ((code: string) => void) | undefined,
}))

vi.mock('../apps/meetings/api', async importOriginal => {
  const actual = await importOriginal<typeof import('../apps/meetings/api')>()
  return { ...actual, meetingsApi: apiMocks }
})

interface SttOptions {
  meetingId: string
  onCaption: (text: string) => void
  onFinal?: (text: string) => string | boolean | void
  onError?: (code: string) => void
}

vi.mock('../apps/meetings/hooks/useMeetingTranscription', async importOriginal => {
  const actual = await importOriginal<
    typeof import('../apps/meetings/hooks/useMeetingTranscription')
  >()
  return {
    ...actual,
    useMeetingTranscription: (opts: SttOptions) => {
      stt.onCaption = opts.onCaption
      stt.onFinal = opts.onFinal
      stt.onError = opts.onError
      return { active: stt.active, start: stt.start, stop: stt.stop, supported: true }
    },
  }
})

import { MeetingsApiError, type AgentDef, type MeetingMeta, type MeetingsConfig } from '../apps/meetings/api'
import { useMeetingSession } from '../apps/meetings/hooks/useMeetingSession'

type SessionProps = Parameters<typeof useMeetingSession>[0]

const AGENTS: AgentDef[] = [
  { id: 'note-taker', name: 'Note Taker', widget_type: 'markdown', enabled_by_default: true },
  { id: 'sketch-artist', name: 'Sketch Artist', widget_type: 'html', enabled_by_default: false },
  { id: 'summarizer', name: 'Summarizer', widget_type: 'markdown', enabled_by_default: true },
]

// Poll intervals are deliberately far longer than any test, so a status that
// enables polling never fires a second fetch mid-assertion.
const CONFIG = {
  default_preset: 'standup',
  presets: { standup: { enabled_agents: ['note-taker'] } },
  meeting_agents: AGENTS,
  poll_interval_active: 600_000,
  poll_interval_idle: 600_000,
} as unknown as MeetingsConfig

function meta(overrides: Partial<MeetingMeta> = {}): MeetingMeta {
  return {
    event_id: 'weekly_sync',
    title: 'Weekly Sync',
    status: 'idle',
    ...overrides,
  } as MeetingMeta
}

function mount(overrides: Partial<SessionProps> = {}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const notify = overrides.notify ?? vi.fn()
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
  const initialProps: SessionProps = {
    eventId: overrides.eventId ?? 'weekly:sync',
    fallbackTitle: overrides.fallbackTitle,
    config: 'config' in overrides ? overrides.config : CONFIG,
    notify,
  }
  const view = renderHook((props: SessionProps) => useMeetingSession(props), {
    wrapper,
    initialProps,
  })
  return { ...view, notify, queryClient, initialProps }
}

/** Resolved once the init + meta + outputs queries have settled. */
async function mountLoaded(overrides: Partial<SessionProps> = {}) {
  const view = mount(overrides)
  await waitFor(() => expect(view.result.current.loading).toBe(false))
  await waitFor(() => expect(apiMocks.outputs).toHaveBeenCalled())
  return view
}

beforeEach(() => {
  vi.clearAllMocks()
  stt.active = false
  stt.onCaption = undefined
  stt.onFinal = undefined
  stt.onError = undefined
  apiMocks.init.mockResolvedValue({ meeting_id: 'weekly_sync', meta: meta() })
  apiMocks.meeting.mockResolvedValue({ meta: meta(), live: null })
  apiMocks.outputs.mockResolvedValue({ outputs: {}, tasks: [] })
  for (const key of [
    'start', 'setStatus', 'stop', 'mute', 'toggleAgent', 'dispatch', 'message',
    'resetAgents', 'attachments', 'addTask', 'updateTask', 'deleteTask', 'fileTask',
    'reviewTask',
  ] as const) {
    apiMocks[key].mockResolvedValue({})
  }
})

afterEach(() => {
  cleanup()
})

describe('useMeetingSession server state', () => {
  it('normalizes the event id and seeds the meeting before reading it', async () => {
    const view = await mountLoaded()

    // A calendar event id becomes a path segment, so the colon is normalized.
    expect(view.result.current.meetingId).toBe('weekly_sync')
    // No fallback title given, so the catalog's placeholder is used.
    expect(apiMocks.init).toHaveBeenCalledWith(
      'weekly_sync',
      i18nT('apps.meetings.session.untitled'),
    )
    expect(apiMocks.meeting).toHaveBeenCalledWith('weekly_sync')
    expect(view.result.current.error).toBeNull()
    expect(view.result.current.outputs).toEqual({})
    expect(view.result.current.tasks).toEqual([])
    expect(view.result.current.agentsPaused).toBe(false)
    expect(view.result.current.syncing).toBe(false)
  })

  it('seeds with the caller-supplied title when there is one', async () => {
    await mountLoaded({ fallbackTitle: 'Retrospective' })
    expect(apiMocks.init).toHaveBeenCalledWith('weekly_sync', 'Retrospective')
  })

  it('derives the roster from the selected preset when the meeting has none', async () => {
    const view = await mountLoaded()

    expect(view.result.current.selectedPreset).toBe('standup')
    expect(view.result.current.enabledIds).toEqual(['note-taker'])
    expect(view.result.current.enabledAgents).toEqual([AGENTS[0]])
    expect(view.result.current.agents).toEqual(AGENTS)
    expect(view.result.current.mutedAgents).toEqual([])
  })

  it('prefers the roster, mutes and pause flag persisted on the meeting', async () => {
    apiMocks.meeting.mockResolvedValue({
      meta: meta({ agents_enabled: ['sketch-artist'], muted_agents: ['note-taker'] }),
      live: { agents_paused: true },
    })
    const view = await mountLoaded()

    await waitFor(() => expect(view.result.current.enabledIds).toEqual(['sketch-artist']))
    expect(view.result.current.enabledAgents).toEqual([AGENTS[1]])
    expect(view.result.current.mutedAgents).toEqual(['note-taker'])
    expect(view.result.current.agentsPaused).toBe(true)
    expect(view.result.current.live).toEqual({ agents_paused: true })
  })

  it('exposes the outputs and action items the server returned', async () => {
    apiMocks.outputs.mockResolvedValue({
      outputs: { 'note-taker': '# Notes' },
      tasks: [{ id: 't1', description: 'Follow up', review_status: 'pending' }],
    })
    const view = await mountLoaded()

    await waitFor(() => expect(view.result.current.tasks).toHaveLength(1))
    expect(view.result.current.outputs).toEqual({ 'note-taker': '# Notes' })
  })

  it('surfaces an init failure and never reads the meeting', async () => {
    apiMocks.init.mockRejectedValue(new Error('seed failed'))
    const view = mount()

    // The init query carries its own `retry: 1`, which the client-level
    // `retry: false` does not override, so the failure surfaces one backoff later.
    await waitFor(
      () => expect(view.result.current.error).toBeInstanceOf(Error),
      { timeout: 6000 },
    )
    expect(view.result.current.error?.message).toBe('seed failed')
    // The meta and outputs queries are gated on init succeeding.
    expect(apiMocks.meeting).not.toHaveBeenCalled()
    expect(apiMocks.outputs).not.toHaveBeenCalled()
    expect(view.result.current.status).toBe('idle')
  })

  it.each(['active', 'paused', 'reviewing', 'ended'] as const)(
    'reports the %s status the server holds',
    async status => {
      apiMocks.meeting.mockResolvedValue({ meta: meta({ status }), live: null })
      const view = await mountLoaded()
      await waitFor(() => expect(view.result.current.status).toBe(status))
    },
  )

  it('refetches both queries on an explicit refresh', async () => {
    const view = await mountLoaded()
    apiMocks.meeting.mockClear()
    apiMocks.outputs.mockClear()

    await act(async () => {
      view.result.current.refresh()
    })

    await waitFor(() => expect(apiMocks.meeting).toHaveBeenCalled())
    await waitFor(() => expect(apiMocks.outputs).toHaveBeenCalled())
  })
})

describe('useMeetingSession preset selection', () => {
  it('applies the configured default once a late config arrives', async () => {
    const view = mount({ config: undefined })
    await waitFor(() => expect(view.result.current.loading).toBe(false))
    expect(view.result.current.selectedPreset).toBe('')

    view.rerender({ ...view.initialProps, config: CONFIG })

    await waitFor(() => expect(view.result.current.selectedPreset).toBe('standup'))
  })

  it('never overwrites a preset the user already chose', async () => {
    const view = mount({ config: undefined })
    await waitFor(() => expect(view.result.current.loading).toBe(false))

    act(() => view.result.current.setSelectedPreset('design'))
    view.rerender({ ...view.initialProps, config: CONFIG })

    await waitFor(() => expect(view.result.current.selectedPreset).toBe('design'))
  })

  it('leaves the selection alone when the config names no default', async () => {
    const bare = { meeting_agents: AGENTS } as unknown as MeetingsConfig
    const view = await mountLoaded({ config: bare })
    expect(view.result.current.selectedPreset).toBe('')
    // The roster defaults stand in for the missing preset.
    expect(view.result.current.enabledIds).toEqual(['note-taker', 'summarizer'])
  })
})

describe('useMeetingSession transcription binding', () => {
  it('starts the microphone when the meeting is active', async () => {
    apiMocks.meeting.mockResolvedValue({ meta: meta({ status: 'active' }), live: null })
    await mountLoaded()

    await waitFor(() => expect(stt.start).toHaveBeenCalled())
    expect(stt.stop).not.toHaveBeenCalled()
  })

  it('stops the microphone when the meeting is not active', async () => {
    stt.active = true
    apiMocks.meeting.mockResolvedValue({ meta: meta({ status: 'paused' }), live: null })
    await mountLoaded()

    await waitFor(() => expect(stt.stop).toHaveBeenCalled())
    expect(stt.start).not.toHaveBeenCalled()
  })

  it('restarts a socket that dropped while the meeting stayed active', async () => {
    stt.active = true
    apiMocks.meeting.mockResolvedValue({ meta: meta({ status: 'active' }), live: null })
    const view = await mountLoaded()
    await waitFor(() => expect(view.result.current.status).toBe('active'))
    expect(stt.start).not.toHaveBeenCalled()

    // The socket closes cleanly: the transcription hook clears `active` while the
    // meeting status is unchanged. Keying on status alone left transcription dead
    // for the rest of the meeting.
    stt.active = false
    view.rerender(view.initialProps)

    await waitFor(() => expect(stt.start).toHaveBeenCalled())
  })

  it('publishes the live caption', async () => {
    const view = await mountLoaded()

    act(() => stt.onCaption?.('the quarter is closing'))

    expect(view.result.current.caption).toBe('the quarter is closing')
  })

  it('dispatches only the new words of a growing final', async () => {
    await mountLoaded()

    expect(stt.onFinal?.('yes')).toBe('yes')
    // An exact repeat inside the dedup window contributes nothing.
    expect(stt.onFinal?.('yes')).toBe(false)
    // A revised-upward final contributes only its suffix.
    expect(stt.onFinal?.('yes please')).toBe('please')
    expect(stt.onFinal?.('next topic')).toBe('next topic')
  })

  it.each([
    ['dispatch', 'apps.meetings.session.sttDispatchFailed'],
    ['unsupported', 'apps.meetings.session.sttUnsupported'],
    ['microphone', 'apps.meetings.session.sttMicDenied'],
    ['worklet', 'apps.meetings.session.sttWorkletFailed'],
    ['connection', 'apps.meetings.session.sttConnectionFailed'],
    ['disconnected', 'apps.meetings.session.sttDisconnected'],
  ] as const)('reports the %s transcription failure with its own message', async (code, key) => {
    const view = await mountLoaded()

    act(() => stt.onError?.(code))

    expect(view.notify).toHaveBeenCalledWith(i18nT(key), { type: 'error' })
  })

  it('falls back to the generic message for an unrecognized failure code', async () => {
    const view = await mountLoaded()

    act(() => stt.onError?.('meteor-strike'))

    expect(view.notify).toHaveBeenCalledWith(
      i18nT('apps.meetings.session.sttUnavailable'),
      { type: 'error' },
    )
  })
})

describe('useMeetingSession lifecycle actions', () => {
  it('starts a meeting with the title, preset and roster it knows', async () => {
    const view = await mountLoaded()

    await act(async () => {
      view.result.current.actions.start()
    })

    await waitFor(() => expect(apiMocks.start).toHaveBeenCalledWith('weekly_sync', {
      title: 'Weekly Sync',
      preset: 'standup',
      agents_enabled: ['note-taker'],
      muted_agents: [],
      restart: false,
    }))
    expect(view.notify).toHaveBeenCalledWith(
      i18nT('apps.meetings.session.started'),
      { type: 'success' },
    )
  })

  it('omits the roster entirely while the config has not arrived', async () => {
    // `[]` is an explicit empty roster to the server, not "unknown" — sending it
    // persisted "run no agents" and the meeting produced nothing.
    const view = mount({ config: undefined })
    await waitFor(() => expect(view.result.current.loading).toBe(false))

    await act(async () => {
      view.result.current.actions.start()
    })

    await waitFor(() => expect(apiMocks.start).toHaveBeenCalled())
    expect(apiMocks.start.mock.calls[0][1]).toMatchObject({ agents_enabled: undefined })
    expect(Object.keys(apiMocks.start.mock.calls[0][1] as object)).toContain('agents_enabled')
  })

  it('asks for a restart when the meeting already ended', async () => {
    apiMocks.meeting.mockResolvedValue({ meta: meta({ status: 'ended' }), live: null })
    const view = await mountLoaded()
    await waitFor(() => expect(view.result.current.status).toBe('ended'))

    await act(async () => {
      view.result.current.actions.start()
    })

    await waitFor(() => expect(apiMocks.start).toHaveBeenCalled())
    expect(apiMocks.start.mock.calls[0][1]).toMatchObject({ restart: true })
  })

  it.each([
    ['pause', 'paused'],
    ['resume', 'active'],
    ['review', 'reviewing'],
    ['backToMeeting', 'paused'],
  ] as const)('%s moves the meeting to %s', async (action, next) => {
    const view = await mountLoaded()

    await act(async () => {
      view.result.current.actions[action]()
    })

    await waitFor(() => expect(apiMocks.setStatus).toHaveBeenCalledWith('weekly_sync', next))
  })

  it('ends the meeting and refreshes the meeting list', async () => {
    const view = await mountLoaded()
    const invalidate = vi.spyOn(view.queryClient, 'invalidateQueries')

    await act(async () => {
      view.result.current.actions.stop()
    })

    await waitFor(() => expect(apiMocks.stop).toHaveBeenCalledWith('weekly_sync'))
    expect(view.notify).toHaveBeenCalledWith(
      i18nT('apps.meetings.session.ended'),
      { type: 'info' },
    )
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['meetings', 'list'] })
  })

  it('mutes and unmutes a single agent', async () => {
    const view = await mountLoaded()

    await act(async () => {
      view.result.current.actions.mute('note-taker', true)
    })

    await waitFor(() =>
      expect(apiMocks.mute).toHaveBeenCalledWith('weekly_sync', 'note-taker', true))
  })

  it('toggles an agent on the meeting', async () => {
    const view = await mountLoaded()

    await act(async () => {
      view.result.current.actions.toggleAgent('summarizer', false)
    })

    await waitFor(() =>
      expect(apiMocks.toggleAgent).toHaveBeenCalledWith('weekly_sync', 'summarizer', false))
  })

  it('broadcasts to the listening agents as chat', async () => {
    // The segment is load-bearing: the source commits it into the transcript
    // cache, so a response without one routes the mutation into onError.
    apiMocks.dispatch.mockResolvedValueOnce({
      dispatched: 2,
      text: 'please summarize',
      segment: {
        id: 'broadcast-1',
        timestamp: '2026-01-01T00:00:00Z',
        source: 'typed',
        text: 'please summarize',
      },
    })
    const view = await mountLoaded()

    await act(async () => {
      view.result.current.actions.broadcast('please summarize')
    })

    await waitFor(() =>
      expect(apiMocks.dispatch).toHaveBeenCalledWith('weekly_sync', 'please summarize', true))
    expect(view.notify).toHaveBeenCalledWith(
      i18nT('apps.meetings.session.broadcastSent'),
      { type: 'info' },
    )
  })

  it('messages one agent directly', async () => {
    const view = await mountLoaded()

    await act(async () => {
      view.result.current.actions.messageAgent('note-taker', 'tighten the notes')
    })

    await waitFor(() =>
      expect(apiMocks.message).toHaveBeenCalledWith(
        'weekly_sync', 'note-taker', 'tighten the notes'))
  })

  it('resumes every paused agent', async () => {
    const view = await mountLoaded()

    await act(async () => {
      view.result.current.actions.resetAgents()
    })

    await waitFor(() => expect(apiMocks.resetAgents).toHaveBeenCalledWith('weekly_sync'))
    expect(view.notify).toHaveBeenCalledWith(
      i18nT('apps.meetings.session.agentsResumed'),
      { type: 'info' },
    )
  })

  it('adds and removes an attachment', async () => {
    const view = await mountLoaded()

    await act(async () => {
      view.result.current.actions.addAttachment('https://example.com/doc', 'Design')
    })
    await waitFor(() => expect(apiMocks.attachments).toHaveBeenCalledWith('weekly_sync', {
      action: 'add',
      attachments: [{ type: 'url', url: 'https://example.com/doc', label: 'Design' }],
    }))

    await act(async () => {
      view.result.current.actions.removeAttachment(2)
    })
    await waitFor(() => expect(apiMocks.attachments).toHaveBeenCalledWith('weekly_sync', {
      action: 'remove',
      index: 2,
    }))
  })
})

describe('useMeetingSession action items', () => {
  it('adds, edits and deletes an action item', async () => {
    const view = await mountLoaded()

    await act(async () => {
      view.result.current.actions.addTask('ship the report')
    })
    await waitFor(() =>
      expect(apiMocks.addTask).toHaveBeenCalledWith('weekly_sync', 'ship the report'))

    await act(async () => {
      view.result.current.actions.updateTask('t1', { assignee: 'zezhexu' })
    })
    await waitFor(() => expect(apiMocks.updateTask).toHaveBeenCalledWith(
      'weekly_sync', 't1', { assignee: 'zezhexu' }))

    await act(async () => {
      view.result.current.actions.deleteTask('t1')
    })
    await waitFor(() => expect(apiMocks.deleteTask).toHaveBeenCalledWith('weekly_sync', 't1'))
  })

  it('files an action item and reports it', async () => {
    const view = await mountLoaded()

    await act(async () => {
      view.result.current.actions.fileTask('t7')
    })

    await waitFor(() => expect(apiMocks.fileTask).toHaveBeenCalledWith('weekly_sync', 't7'))
    expect(view.notify).toHaveBeenCalledWith(
      i18nT('apps.meetings.session.taskFiled'),
      { type: 'success' },
    )
  })

  it('names the action item currently being filed', async () => {
    let release: () => void = () => undefined
    apiMocks.fileTask.mockImplementation(
      () => new Promise(resolve => { release = () => resolve({}) }),
    )
    const view = await mountLoaded()

    act(() => {
      view.result.current.actions.fileTask('t9')
    })
    await waitFor(() => expect(view.result.current.pending.filing).toBe('t9'))

    // Resolved before the test ends: an in-flight promise escaping the test is
    // what turns a green suite into a non-zero exit.
    await act(async () => {
      release()
    })
    await waitFor(() => expect(view.result.current.pending.filing).toBeNull())
  })

  it('archives and unarchives an action item', async () => {
    const view = await mountLoaded()

    await act(async () => {
      view.result.current.actions.archiveTask('t2')
    })
    await waitFor(() =>
      expect(apiMocks.reviewTask).toHaveBeenCalledWith('weekly_sync', 't2', 'archived'))

    await act(async () => {
      view.result.current.actions.unarchiveTask('t2')
    })
    await waitFor(() =>
      expect(apiMocks.reviewTask).toHaveBeenCalledWith('weekly_sync', 't2', 'pending'))
  })
})

describe('useMeetingSession failure reporting', () => {
  it.each([
    ['start', 'start', 'apps.meetings.session.startFailed'],
    ['pause', 'setStatus', 'apps.meetings.session.statusFailed'],
    ['stop', 'stop', 'apps.meetings.session.stopFailed'],
    ['broadcast', 'dispatch', 'apps.meetings.session.broadcastFailed'],
    ['messageAgent', 'message', 'apps.meetings.session.messageFailed'],
    ['toggleAgent', 'toggleAgent', 'apps.meetings.session.agentToggleFailed'],
    ['addAttachment', 'attachments', 'apps.meetings.session.attachmentFailed'],
    ['addTask', 'addTask', 'apps.meetings.session.taskAddFailed'],
    ['fileTask', 'fileTask', 'apps.meetings.session.taskFileFailed'],
  ] as const)('reports a failed %s with its own message', async (action, mockName, key) => {
    apiMocks[mockName].mockRejectedValue(new Error('boom'))
    const view = await mountLoaded()

    await act(async () => {
      // Every one of these takes zero, one or two arguments; the extras are
      // ignored by the shorter signatures.
      const call = view.result.current.actions[action] as (
        a?: string, b?: string,
      ) => void
      call('note-taker', 'text')
    })

    await waitFor(() =>
      expect(view.notify).toHaveBeenCalledWith(i18nT(key), { type: 'error' }))
  })

  it('explains a 409 as another meeting already running', async () => {
    apiMocks.start.mockRejectedValue(new MeetingsApiError('conflict', 409))
    const view = await mountLoaded()

    await act(async () => {
      view.result.current.actions.start()
    })

    await waitFor(() => expect(view.notify).toHaveBeenCalledWith(
      i18nT('apps.meetings.session.anotherMeetingActive'),
      { type: 'error' },
    ))
  })

  it('keeps the generic message for a non-conflict api error', async () => {
    apiMocks.start.mockRejectedValue(new MeetingsApiError('server on fire', 500))
    const view = await mountLoaded()

    await act(async () => {
      view.result.current.actions.start()
    })

    await waitFor(() => expect(view.notify).toHaveBeenCalledWith(
      i18nT('apps.meetings.session.startFailed'),
      { type: 'error' },
    ))
  })
})

describe('useMeetingSession chat view', () => {
  it('adds and then removes an agent from the chat view', async () => {
    const view = await mountLoaded()

    act(() => view.result.current.toggleChatView('note-taker'))
    expect(view.result.current.chatViewAgents).toEqual(['note-taker'])

    act(() => view.result.current.toggleChatView('summarizer'))
    expect(view.result.current.chatViewAgents).toEqual(['note-taker', 'summarizer'])

    act(() => view.result.current.toggleChatView('note-taker'))
    expect(view.result.current.chatViewAgents).toEqual(['summarizer'])
  })
})
