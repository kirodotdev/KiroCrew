// Everything one meeting's view needs: server state via React Query, the
// transcription stream, and the lifecycle mutations.
//
// Upstream did this with ~15 useState + useEffect + manual-fetch pairs. Here the
// server state is React Query (per `website/AGENTS.md` "Data Fetching") and only
// genuinely local UI state (which agents are shown as chat, the live caption)
// stays in useState.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { i18nT } from '../../../i18n/t'
import {
  MeetingsApiError,
  meetingsApi,
  safeMeetingId,
  type AgentDef,
  type MeetingMeta,
  type MeetingStatus,
  type MeetingsConfig,
  type Task,
  type TranscriptResponse,
  type TranscriptSegment,
} from '../api'
import { useMeetingTranscription } from './useMeetingTranscription'

/** Transcript segments arrive with overlap; a repeat inside this window is dropped. */
const DEDUP_WINDOW_MS = 5000

/**
 * Poll cadence while a start is in flight.
 *
 * Deliberately faster than `poll_interval_active`: this window is short and the
 * whole point is to notice `active` promptly, so the Live badge and the meeting
 * controls come alive instead of looking dead. Not configurable, because it is
 * not a steady-state cost — at most a handful of requests, once per start.
 */
const START_POLL_MS = 1000

/**
 * Transcription failure code -> catalog key.
 *
 * FILE SCOPE, not inline in the handler that indexes it: `check-i18n-keys.mjs`
 * collects only file-scope consts, so the same map declared inside the callback
 * resolved to nothing and all five keys went unverified — exempt from the
 * existence, parity and dead-key checks. Hoisting it is what makes them checked.
 */
const STT_ERROR_KEY = {
  // Not a transcription fault: the audio was recognized, but the segment never
  // reached the agents, so the notes and tasks have a gap. Distinct message
  // because "transcription is unavailable" would misdescribe it — the mic is fine.
  dispatch: 'apps.meetings.session.sttDispatchFailed',
  unsupported: 'apps.meetings.session.sttUnsupported',
  microphone: 'apps.meetings.session.sttMicDenied',
  worklet: 'apps.meetings.session.sttWorkletFailed',
  connection: 'apps.meetings.session.sttConnectionFailed',
  disconnected: 'apps.meetings.session.sttDisconnected',
} as const

/** True when *text* repeats, contains, or is contained by the previous segment. */
export function isDuplicateSegment(
  text: string,
  previous: { text: string; ts: number },
  now: number,
): boolean {
  if (!text.trim()) return true
  if (now - previous.ts >= DEDUP_WINDOW_MS) return false
  if (!previous.text) return false
  return text === previous.text || previous.text.includes(text) || text.includes(previous.text)
}

/**
 * The part of *text* the agents have not already been sent, or "" for a repeat.
 *
 * STT emits a GROWING final: `"yes"` and then `"yes please"` within the dedup
 * window. `isDuplicateSegment` correctly says the second overlaps the first — but
 * suppressing it outright discarded the new words, so the notes and task
 * extraction never saw "please". Whole clauses vanished this way whenever the
 * recognizer revised upward, which for short affirmations is most of the time.
 *
 * The extension case is therefore split from the true-repeat case: a segment that
 * STARTS WITH what was already dispatched contributes only its new suffix, and
 * anything else (an exact repeat, or a shorter re-recognition already covered)
 * contributes nothing. Returning the suffix rather than the whole text is what
 * keeps the agents from seeing "yes" twice.
 */
export function newSegmentText(
  text: string,
  previous: { text: string; ts: number },
  now: number,
): string {
  const trimmed = text.trim()
  if (!trimmed) return ''
  if (now - previous.ts >= DEDUP_WINDOW_MS || !previous.text) return trimmed
  if (trimmed === previous.text) return ''
  if (trimmed.startsWith(previous.text)) {
    // A growing final: send only what was added.
    return trimmed.slice(previous.text.length).trim()
  }
  // A shorter or re-worded re-recognition of the same speech. Already covered by
  // what was dispatched, so nothing new — and NOT worth guessing a diff for, since
  // a wrong guess sends the agents a fragment out of context.
  if (previous.text.includes(trimmed)) return ''
  return trimmed
}

/** Merge cursor pages and immediate dispatch responses by durable segment id. */
export function mergeTranscriptSegments(
  current: readonly TranscriptSegment[],
  incoming: readonly TranscriptSegment[],
): TranscriptSegment[] {
  if (incoming.length === 0) return [...current]
  const seen = new Set(current.map(segment => segment.id))
  const merged = [...current]
  for (const segment of incoming) {
    if (seen.has(segment.id)) continue
    seen.add(segment.id)
    merged.push(segment)
  }
  return merged
}

/** Reconcile a canonical cursor page with responses appended optimistically. */
export function reconcileTranscriptPage(
  current: readonly TranscriptSegment[],
  page: readonly TranscriptSegment[],
): TranscriptSegment[] {
  if (page.length === 0) return [...current]
  const confirmedIds = new Set(page.map(segment => segment.id))
  return [
    ...current.filter(segment => !confirmedIds.has(segment.id)),
    ...page,
  ]
}

/** Which agents a preset (or the roster's defaults) turns on. */
export function resolveEnabledAgents(
  presetName: string,
  config: MeetingsConfig | undefined,
  agents: AgentDef[],
): string[] {
  const preset = presetName ? config?.presets?.[presetName] : undefined
  if (preset?.enabled_agents?.length) return preset.enabled_agents
  return agents.filter(a => a.enabled_by_default !== false).map(a => a.id)
}

/** Which lifecycle transitions the UI offers from a given status. */
export const ALLOWED_TRANSITIONS: Record<MeetingStatus, MeetingStatus[]> = {
  idle: ['active'],
  active: ['paused', 'reviewing'],
  paused: ['active', 'reviewing'],
  reviewing: ['paused', 'ended'],
  ended: ['active'],
}

/**
 * Poll cadence for a meeting's metadata query, or `false` for "do not poll".
 *
 * The `startInFlight` rule is the load-bearing one. `POST /start` does not answer
 * until EVERY agent has been initialized, and that is a sequence of awaited agent
 * dispatches — tens of seconds with the default roster (measured: ~46s for three).
 * The meeting is already `active` on the server for almost all of that, because the
 * status is persisted up front, BEFORE the dispatches begin. So the wait gates
 * nothing except this client's knowledge of it.
 *
 * Without the rule that was enough to look like a broken button: polling is off
 * while `idle`, so with no poll and no resolved mutation the UI sat on a stale
 * `idle` for the whole initialization — Start greyed out (it is disabled while the
 * mutation is pending), no Live badge, and, because the microphone is bound to
 * `status`, no permission prompt and no capture at all. The only way through was a
 * manual reload, whose fresh fetch saw `active` and brought the meeting to life.
 * Users reasonably concluded that recording does not start without a reload.
 *
 * Checked LAST so a known status always wins: once a poll lands and `status` is
 * `active`, the caller's configured cadence takes over and this rule goes quiet on
 * its own.
 */
export function metaPollInterval(opts: {
  status: MeetingStatus | undefined
  startInFlight: boolean
  activeMs?: number
  idleMs?: number
}): number | false {
  const { status, startInFlight, activeMs, idleMs } = opts
  if (status === 'active') return activeMs ?? 5000
  if (status === 'paused' || status === 'reviewing') return idleMs ?? 30_000
  if (startInFlight) return START_POLL_MS
  return false
}

export function canTransition(from: MeetingStatus, to: MeetingStatus): boolean {
  return ALLOWED_TRANSITIONS[from]?.includes(to) ?? false
}

/**
 * Whether the microphone may open for an `active` meeting.
 *
 * `status === 'active'` alone is NOT dispatch readiness. `POST /start` persists
 * `active` up front and then initializes agents with transcript ingress
 * SUSPENDED — every dispatch in that window is rejected with a 409
 * (`no_active_meeting`, see the backend's `get_for_dispatch`). The fast start
 * poll exists to observe `active` early, so without a readiness gate it opened
 * the microphone tens of seconds before ingress: early finals burned through
 * the short dispatch retry schedule (~4.6s) against a ~46s initialization and
 * were PERMANENTLY lost from the notes and tasks.
 *
 * `ingressReady` is the server's own admission flag, `accepting_dispatches`,
 * reported on every meta poll — the same holder flag the dispatch endpoint
 * itself checks. Gating on the POLLED value rather than on the start
 * mutation's lifecycle makes every client-side inference hole unreachable at
 * once, because the mutation's outcome is not evidence about ingress in either
 * direction: an error settlement can hide a server-side success (the ~46s
 * request is exactly the shape a proxy timeout kills), and a success response
 * can be lost in transit. With the polled flag, the mic simply follows what
 * the server reports: closed while initialization holds ingress shut, open on
 * the first poll after `resume_dispatches` — whatever became of the start
 * request itself. A reload or a second tab lands on the same rule, which also
 * closes the pre-existing hole where a tab that never started the meeting
 * opened its mic mid-initialization.
 *
 * The Live badge and controls still key off `status` and appear within ~1s of
 * Start; only capture waits for the server to report ingress open.
 */
export function canOpenTranscription(opts: {
  status: MeetingStatus
  ingressReady: boolean
  transcriptFull: boolean
}): boolean {
  const { status, ingressReady, transcriptFull } = opts
  return status === 'active' && ingressReady && !transcriptFull
}

interface Options {
  eventId: string
  fallbackTitle?: string
  config: MeetingsConfig | undefined
  notify: (message: string, opts?: { type?: 'info' | 'success' | 'error' }) => void
}

export function useMeetingSession({ eventId, fallbackTitle, config, notify }: Options) {
  const queryClient = useQueryClient()
  const meetingId = useMemo(() => safeMeetingId(eventId), [eventId])
  const scope = ['meetings', meetingId] as const

  const [caption, setCaption] = useState('')
  const [partialTranscript, setPartialTranscript] = useState('')
  const [fullMeetingId, setFullMeetingId] = useState('')
  const transcriptFullNoticeRef = useRef('')
  const [chatViewAgents, setChatViewAgents] = useState<string[]>([])
  const [selectedPreset, setSelectedPreset] = useState(config?.default_preset ?? '')
  // `useState` captures its initial value ONCE, and `config` arrives from a query —
  // so a meeting opened before that resolves kept `''` forever and started with the
  // roster defaults instead of the configured preset, silently omitting whatever
  // agents the preset adds. Applied when the config lands, and only while the user
  // has not chosen one: `selectedPreset` is theirs to set once non-empty, so this
  // must never overwrite a real selection (and must not fire again if the config
  // refetches).
  const presetAppliedRef = useRef(false)
  useEffect(() => {
    const preset = config?.default_preset
    if (!preset || presetAppliedRef.current) return
    presetAppliedRef.current = true
    setSelectedPreset(current => current || preset)
  }, [config?.default_preset])
  const lastSegmentRef = useRef({ text: '', ts: 0 })

  // The folder + seed files must exist before anything reads them, so this is a
  // one-shot init the rest of the queries wait on.
  const initQuery = useQuery({
    queryKey: [...scope, 'init'],
    queryFn: () => meetingsApi.init(meetingId, fallbackTitle || i18nT('apps.meetings.session.untitled')),
    staleTime: Infinity,
    retry: 1,
  })

  // Whether a start is waiting on the server. Drives the poll that lets the UI see
  // `active` while `POST /start` is still initializing agents — see
  // `metaPollInterval`, which explains why that wait is long and why it matters.
  // The MICROPHONE deliberately does not key off this flag: it follows the polled
  // `live.accepting_dispatches` instead (see `canOpenTranscription`), so the
  // mutation's fate — settled, failed, or its response lost in transit — cannot
  // wedge capture in either direction.
  const [startInFlight, setStartInFlight] = useState(false)

  const metaQuery = useQuery({
    queryKey: [...scope, 'meta'],
    queryFn: () => meetingsApi.meeting(meetingId),
    enabled: initQuery.isSuccess,
    refetchInterval: query =>
      metaPollInterval({
        status: query.state.data?.meta?.status,
        startInFlight,
        activeMs: config?.poll_interval_active,
        idleMs: config?.poll_interval_idle,
      }),
  })

  const meta: MeetingMeta | undefined = metaQuery.data?.meta
  const status: MeetingStatus = meta?.status ?? 'idle'
  const live = metaQuery.data?.live ?? null
  // The server's own dispatch-admission flag, straight off the poll. `=== true`
  // rather than truthy-or-default: a missing `live` (no session installed — a
  // gateway restart left the meeting `active` on disk with nothing live) means a
  // dispatch CANNOT land, so unknown must read as not-ready.
  const ingressReady = live?.accepting_dispatches === true

  const outputsQuery = useQuery({
    queryKey: [...scope, 'outputs'],
    queryFn: () => meetingsApi.outputs(meetingId),
    enabled: initQuery.isSuccess,
    refetchInterval: status === 'active'
      ? (config?.poll_interval_active ?? 5000)
      : status === 'paused' || status === 'reviewing'
        ? (config?.poll_interval_idle ?? 30_000)
        : false,
  })

  const transcriptKey = [...scope, 'transcript'] as const
  const transcriptQuery = useQuery({
    queryKey: transcriptKey,
    queryFn: async () => {
      const current = queryClient.getQueryData<TranscriptResponse>(transcriptKey)
      const page = await meetingsApi.transcript(meetingId, current?.next_cursor ?? 0)
      if (!current) return page
      return {
        // Dispatch responses are optimistic: concurrent requests can resolve in a
        // different order from the durable append. The cursor page is canonical,
        // so overlapping optimistic rows are removed and reinserted in file order.
        segments: reconcileTranscriptPage(current.segments, page.segments),
        next_cursor: page.next_cursor,
      }
    },
    enabled: initQuery.isSuccess,
    refetchInterval: status === 'active'
      ? (config?.poll_interval_active ?? 5000)
      : status === 'paused' || status === 'reviewing'
        ? (config?.poll_interval_idle ?? 30_000)
        : false,
  })

  const agents = config?.meeting_agents ?? []
  const enabledIds = meta?.agents_enabled ?? resolveEnabledAgents(selectedPreset, config, agents)
  // Is `enabledIds` a real roster, or the empty list that a not-yet-loaded config
  // produces? The two are indistinguishable downstream but mean opposite things to
  // the server, so the question has to be answered here.
  //
  // `resolveEnabledAgents` derives its answer from `config.meeting_agents`, so
  // before the config query resolves it can only return `[]` — and `[]` is not
  // "unknown" to the start endpoint, it is an explicit EMPTY ROSTER (see
  // `field_str_list`, where absent and `[]` are deliberately different). Starting a
  // meeting in that window therefore persisted "run no agents", and the note-taker,
  // the diagram agent and every other configured agent never ran: the meeting
  // recorded audio and produced nothing.
  //
  // Keyed on the config actually having arrived rather than on `enabledIds.length`,
  // because an empty roster the USER chose is a legitimate state that must still be
  // sent verbatim.
  const rosterIsKnown = Boolean(meta?.agents_enabled) || Boolean(config)
  const enabledAgents = useMemo(
    () => agents.filter(a => enabledIds.includes(a.id)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [agents, enabledIds.join(',')],
  )
  const mutedAgents = meta?.muted_agents ?? []
  const outputs = outputsQuery.data?.outputs ?? {}
  const tasks: Task[] = outputsQuery.data?.tasks ?? []
  const transcript: TranscriptSegment[] = transcriptQuery.data?.segments ?? []
  const transcriptFull = fullMeetingId === meetingId

  const commitTranscriptSegment = useCallback(
    (segment: TranscriptSegment) => {
      queryClient.setQueryData<TranscriptResponse>(
        transcriptKey,
        current => {
          const segments = current?.segments ?? []
          return {
            segments: mergeTranscriptSegments(segments, [segment]),
            next_cursor: current?.next_cursor ?? 0,
          }
        },
      )
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [queryClient, meetingId],
  )

  const markTranscriptFull = useCallback(() => {
    setFullMeetingId(meetingId)
    if (transcriptFullNoticeRef.current === meetingId) return
    transcriptFullNoticeRef.current = meetingId
    notify(i18nT('apps.meetings.transcript.full'), { type: 'error' })
  }, [meetingId, notify])

  const invalidate = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: [...scope, 'meta'] })
    void queryClient.invalidateQueries({ queryKey: [...scope, 'outputs'] })
    void queryClient.invalidateQueries({ queryKey: [...scope, 'transcript'] })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [queryClient, meetingId])

  // ── transcription ─────────────────────────────────────────────────────────

  const onCaption = useCallback((text: string) => setCaption(text), [])

  /** Returns `false` for an overlapping repeat, which suppresses its dispatch. */
  const onSegment = useCallback((text: string): string | false => {
    const now = Date.now()
    const fresh = newSegmentText(text, lastSegmentRef.current, now)
    if (!fresh) return false
    // Track the FULL text, not the suffix: the next segment's overlap is against
    // everything recognized so far, not just the part last dispatched.
    lastSegmentRef.current = { text: text.trim(), ts: now }
    return fresh
  }, [])

  const onTranscriptionError = useCallback(
    (code: string) => {
      if (code === 'transcript_full') {
        markTranscriptFull()
        return
      }
      // Indexed INSIDE the `i18nT(...)` call rather than via a local `const key`:
      // the gate collects only file-scope consts, so a function-local binding is
      // opaque to it however resolvable its initializer is.
      notify(
        i18nT(
          code in STT_ERROR_KEY
            ? STT_ERROR_KEY[code as keyof typeof STT_ERROR_KEY]
            : 'apps.meetings.session.sttUnavailable',
        ),
        { type: 'error' },
      )
    },
    [markTranscriptFull, notify],
  )

  const transcription = useMeetingTranscription({
    meetingId,
    onCaption,
    onFinal: onSegment,
    onPartial: setPartialTranscript,
    onCommitted: commitTranscriptSegment,
    onError: onTranscriptionError,
  })

  // Bind the microphone to the meeting's status: recording exactly while active
  // AND dispatch-ready — see `canOpenTranscription` for why `active` alone is not
  // enough while a start is still initializing agents.
  //
  // Keyed on `transcription.active` as WELL as `status`. Keying on `status` alone
  // made the binding one-directional: if the socket dropped while the meeting was
  // active, the transcription hook's `cleanup()` set `active` false, and since
  // `status` had not changed this effect never re-ran — so transcription stayed
  // dead for the rest of the meeting while the UI still showed Live, and every
  // word after that point was missing from the notes. The hook's own watchdog
  // reconnects a STALLED socket, but a socket that closes cleanly and unexpectedly
  // lands here instead.
  //
  // Safe against a restart loop: `start()` sets `active` true, which is the
  // condition that stops this branch firing, and a `start()` that fails reports
  // through `onError` rather than flipping `active`.
  const transcriptionRef = useRef(transcription)
  transcriptionRef.current = transcription
  const transcriptionActive = transcription.active
  useEffect(() => {
    const mayOpen = canOpenTranscription({ status, ingressReady, transcriptFull })
    if (mayOpen && !transcriptionRef.current.active) {
      void transcriptionRef.current.start()
    }
    if (!mayOpen && transcriptionRef.current.active) {
      transcriptionRef.current.stop()
    }
  }, [status, ingressReady, transcriptFull, transcriptionActive])

  // ── mutations ─────────────────────────────────────────────────────────────

  /**
   * Notify a mutation failure, special-casing the 409 "another meeting is active".
   *
   * Takes the fallback as TRANSLATED TEXT, not as a catalog key. Passing a key
   * meant the only `i18nT` was `i18nT(fallbackKey)` inside here, which
   * `check-i18n-keys.mjs` cannot resolve — so all nine fallback keys were
   * unverifiable and exempt from the existence, parity and dead-key checks, even
   * though every call site passes a plain literal. Translating at the call site
   * puts them all back under the gate for the cost of nine `i18nT(...)` wrappers.
   */
  const failureNotice = useCallback(
    (error: unknown, fallback: string) => {
      const message =
        error instanceof MeetingsApiError && error.status === 409
          ? i18nT('apps.meetings.session.anotherMeetingActive')
          : fallback
      notify(message, { type: 'error' })
    },
    [notify],
  )

  const startMutation = useMutation({
    mutationFn: (opts: { restart?: boolean }) =>
      meetingsApi.start(meetingId, {
        title: meta?.title || fallbackTitle,
        preset: selectedPreset || undefined,
        // OMITTED, not `[]`, while the roster is still unknown: absent means "use
        // the configured defaults" to the server, which is what an unloaded config
        // should fall back to. See `rosterIsKnown`.
        agents_enabled: rosterIsKnown ? enabledIds : undefined,
        muted_agents: mutedAgents,
        restart: opts.restart,
      }),
    // Opens the polling window BEFORE the request goes out, so the status is
    // observable for the entire time the server spends initializing agents.
    onMutate: () => setStartInFlight(true),
    // `onSettled`, not `onSuccess`: a start that 409s or fails must close the window
    // too, or a meeting that never started would be polled forever. This flag only
    // drives poll cadence — the microphone follows the polled
    // `live.accepting_dispatches`, so nothing here needs to reason about whether an
    // outcome reflects the server's true state.
    onSettled: () => setStartInFlight(false),
    onSuccess: () => {
      notify(i18nT('apps.meetings.session.started'), { type: 'success' })
      invalidate()
    },
    onError: error => failureNotice(error, i18nT('apps.meetings.session.startFailed')),
  })

  const statusMutation = useMutation({
    mutationFn: (next: MeetingStatus) => meetingsApi.setStatus(meetingId, next),
    onSuccess: () => invalidate(),
    onError: error => failureNotice(error, i18nT('apps.meetings.session.statusFailed')),
  })

  const stopMutation = useMutation({
    mutationFn: () => meetingsApi.stop(meetingId),
    onSuccess: () => {
      notify(i18nT('apps.meetings.session.ended'), { type: 'info' })
      invalidate()
      void queryClient.invalidateQueries({ queryKey: ['meetings', 'list'] })
    },
    onError: error => failureNotice(error, i18nT('apps.meetings.session.stopFailed')),
  })

  const muteMutation = useMutation({
    mutationFn: (vars: { agentId: string; muted: boolean }) =>
      meetingsApi.mute(meetingId, vars.agentId, vars.muted),
    onSuccess: () => invalidate(),
  })

  const toggleAgentMutation = useMutation({
    mutationFn: (vars: { agentId: string; enable: boolean }) =>
      meetingsApi.toggleAgent(meetingId, vars.agentId, vars.enable),
    onSuccess: () => invalidate(),
    onError: error => failureNotice(error, i18nT('apps.meetings.session.agentToggleFailed')),
  })

  const broadcastMutation = useMutation({
    mutationFn: (text: string) => meetingsApi.dispatch(meetingId, text, true),
    onSuccess: response => {
      commitTranscriptSegment(response.segment)
      notify(i18nT('apps.meetings.session.broadcastSent'), { type: 'info' })
    },
    onError: error => {
      if (
        error instanceof MeetingsApiError
        && error.status === 413
        && error.code === 'transcript_too_large'
      ) {
        markTranscriptFull()
        return
      }
      failureNotice(error, i18nT('apps.meetings.session.broadcastFailed'))
    },
  })

  const agentMessageMutation = useMutation({
    mutationFn: (vars: { agentId: string; text: string }) =>
      meetingsApi.message(meetingId, vars.agentId, vars.text),
    onSuccess: () => invalidate(),
    onError: error => failureNotice(error, i18nT('apps.meetings.session.messageFailed')),
  })

  const resetAgentsMutation = useMutation({
    mutationFn: () => meetingsApi.resetAgents(meetingId),
    onSuccess: () => {
      notify(i18nT('apps.meetings.session.agentsResumed'), { type: 'info' })
      invalidate()
    },
  })

  const attachmentMutation = useMutation({
    mutationFn: (vars: Parameters<typeof meetingsApi.attachments>[1]) =>
      meetingsApi.attachments(meetingId, vars),
    onSuccess: () => invalidate(),
    onError: error => failureNotice(error, i18nT('apps.meetings.session.attachmentFailed')),
  })

  // ── task mutations ────────────────────────────────────────────────────────

  const addTaskMutation = useMutation({
    mutationFn: (description: string) => meetingsApi.addTask(meetingId, description),
    onSuccess: () => invalidate(),
    onError: error => failureNotice(error, i18nT('apps.meetings.session.taskAddFailed')),
  })

  const updateTaskMutation = useMutation({
    mutationFn: (vars: { taskId: string; fields: Partial<Task> }) =>
      meetingsApi.updateTask(meetingId, vars.taskId, vars.fields),
    onSuccess: () => invalidate(),
  })

  const deleteTaskMutation = useMutation({
    mutationFn: (taskId: string) => meetingsApi.deleteTask(meetingId, taskId),
    onSuccess: () => invalidate(),
  })

  const fileTaskMutation = useMutation({
    mutationFn: (taskId: string) => meetingsApi.fileTask(meetingId, taskId),
    onSuccess: () => {
      notify(i18nT('apps.meetings.session.taskFiled'), { type: 'success' })
      invalidate()
    },
    onError: error => failureNotice(error, i18nT('apps.meetings.session.taskFileFailed')),
  })

  const reviewTaskMutation = useMutation({
    mutationFn: (vars: { taskId: string; reviewStatus: 'pending' | 'archived' }) =>
      meetingsApi.reviewTask(meetingId, vars.taskId, vars.reviewStatus),
    onSuccess: () => invalidate(),
  })

  const toggleChatView = useCallback((agentId: string) => {
    setChatViewAgents(prev =>
      prev.includes(agentId) ? prev.filter(id => id !== agentId) : [...prev, agentId],
    )
  }, [])

  return {
    meetingId,
    meta,
    status,
    live,
    agents,
    enabledAgents,
    enabledIds,
    mutedAgents,
    outputs,
    tasks,
    transcript,
    partialTranscript,
    transcriptFull,
    caption,
    chatViewAgents,
    selectedPreset,
    transcription,
    loading: initQuery.isLoading || metaQuery.isLoading,
    error: (initQuery.error ?? metaQuery.error) as Error | null,
    agentsPaused: Boolean(live?.agents_paused),
    syncing: metaQuery.isFetching || outputsQuery.isFetching || transcriptQuery.isFetching,
    setSelectedPreset,
    toggleChatView,
    refresh: invalidate,
    actions: {
      start: () => startMutation.mutate({ restart: status === 'ended' }),
      pause: () => statusMutation.mutate('paused'),
      resume: () => statusMutation.mutate('active'),
      review: () => statusMutation.mutate('reviewing'),
      backToMeeting: () => statusMutation.mutate('paused'),
      stop: () => stopMutation.mutate(),
      mute: (agentId: string, muted: boolean) => muteMutation.mutate({ agentId, muted }),
      toggleAgent: (agentId: string, enable: boolean) =>
        toggleAgentMutation.mutate({ agentId, enable }),
      broadcast: (text: string) => broadcastMutation.mutate(text),
      messageAgent: (agentId: string, text: string) =>
        agentMessageMutation.mutate({ agentId, text }),
      resetAgents: () => resetAgentsMutation.mutate(),
      addAttachment: (url: string, label: string) =>
        attachmentMutation.mutate({ action: 'add', attachments: [{ type: 'url', url, label }] }),
      removeAttachment: (index: number) =>
        attachmentMutation.mutate({ action: 'remove', index }),
      addTask: (description: string) => addTaskMutation.mutate(description),
      updateTask: (taskId: string, fields: Partial<Task>) =>
        updateTaskMutation.mutate({ taskId, fields }),
      deleteTask: (taskId: string) => deleteTaskMutation.mutate(taskId),
      fileTask: (taskId: string) => fileTaskMutation.mutate(taskId),
      archiveTask: (taskId: string) =>
        reviewTaskMutation.mutate({ taskId, reviewStatus: 'archived' }),
      unarchiveTask: (taskId: string) =>
        reviewTaskMutation.mutate({ taskId, reviewStatus: 'pending' }),
    },
    pending: {
      starting: startMutation.isPending,
      stopping: stopMutation.isPending,
      filing: fileTaskMutation.isPending ? fileTaskMutation.variables : null,
    },
  }
}
