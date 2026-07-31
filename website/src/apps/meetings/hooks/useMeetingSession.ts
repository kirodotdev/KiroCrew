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
} from '../api'
import { useMeetingTranscription } from './useMeetingTranscription'

/** Transcript segments arrive with overlap; a repeat inside this window is dropped. */
const DEDUP_WINDOW_MS = 5000

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

export function canTransition(from: MeetingStatus, to: MeetingStatus): boolean {
  return ALLOWED_TRANSITIONS[from]?.includes(to) ?? false
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
  const [chatViewAgents, setChatViewAgents] = useState<string[]>([])
  const [selectedPreset, setSelectedPreset] = useState(config?.default_preset ?? '')
  const lastSegmentRef = useRef({ text: '', ts: 0 })

  // The folder + seed files must exist before anything reads them, so this is a
  // one-shot init the rest of the queries wait on.
  const initQuery = useQuery({
    queryKey: [...scope, 'init'],
    queryFn: () => meetingsApi.init(meetingId, fallbackTitle || i18nT('apps.meetings.session.untitled')),
    staleTime: Infinity,
    retry: 1,
  })

  const metaQuery = useQuery({
    queryKey: [...scope, 'meta'],
    queryFn: () => meetingsApi.meeting(meetingId),
    enabled: initQuery.isSuccess,
    refetchInterval: query => {
      const status = query.state.data?.meta?.status
      if (status === 'active') return config?.poll_interval_active ?? 5000
      if (status === 'paused' || status === 'reviewing') return config?.poll_interval_idle ?? 30_000
      return false
    },
  })

  const meta: MeetingMeta | undefined = metaQuery.data?.meta
  const status: MeetingStatus = meta?.status ?? 'idle'
  const live = metaQuery.data?.live ?? null

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

  const agents = config?.meeting_agents ?? []
  const enabledIds = meta?.agents_enabled ?? resolveEnabledAgents(selectedPreset, config, agents)
  const enabledAgents = useMemo(
    () => agents.filter(a => enabledIds.includes(a.id)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [agents, enabledIds.join(',')],
  )
  const mutedAgents = meta?.muted_agents ?? []
  const outputs = outputsQuery.data?.outputs ?? {}
  const tasks: Task[] = outputsQuery.data?.tasks ?? []

  const invalidate = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: [...scope, 'meta'] })
    void queryClient.invalidateQueries({ queryKey: [...scope, 'outputs'] })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [queryClient, meetingId])

  // ── transcription ─────────────────────────────────────────────────────────

  const onCaption = useCallback((text: string) => setCaption(text), [])

  /** Returns `false` for an overlapping repeat, which suppresses its dispatch. */
  const onSegment = useCallback((text: string): boolean => {
    const now = Date.now()
    if (isDuplicateSegment(text, lastSegmentRef.current, now)) return false
    lastSegmentRef.current = { text, ts: now }
    return true
  }, [])

  const onTranscriptionError = useCallback(
    (code: string) => {
      const key = {
        unsupported: 'apps.meetings.session.sttUnsupported',
        microphone: 'apps.meetings.session.sttMicDenied',
        worklet: 'apps.meetings.session.sttWorkletFailed',
        connection: 'apps.meetings.session.sttConnectionFailed',
        disconnected: 'apps.meetings.session.sttDisconnected',
      }[code] ?? 'apps.meetings.session.sttUnavailable'
      notify(i18nT(key), { type: 'error' })
    },
    [notify],
  )

  const transcription = useMeetingTranscription({
    meetingId,
    onCaption,
    onFinal: onSegment,
    onError: onTranscriptionError,
  })

  // Bind the microphone to the meeting's status: recording exactly while active.
  const transcriptionRef = useRef(transcription)
  transcriptionRef.current = transcription
  useEffect(() => {
    if (status === 'active' && !transcriptionRef.current.active) {
      void transcriptionRef.current.start()
    }
    if (status !== 'active' && transcriptionRef.current.active) {
      transcriptionRef.current.stop()
    }
  }, [status])

  // ── mutations ─────────────────────────────────────────────────────────────

  const failureNotice = useCallback(
    (error: unknown, fallbackKey: string) => {
      const message =
        error instanceof MeetingsApiError && error.status === 409
          ? i18nT('apps.meetings.session.anotherMeetingActive')
          : i18nT(fallbackKey)
      notify(message, { type: 'error' })
    },
    [notify],
  )

  const startMutation = useMutation({
    mutationFn: (opts: { restart?: boolean }) =>
      meetingsApi.start(meetingId, {
        title: meta?.title || fallbackTitle,
        preset: selectedPreset || undefined,
        agents_enabled: enabledIds,
        muted_agents: mutedAgents,
        restart: opts.restart,
      }),
    onSuccess: () => {
      notify(i18nT('apps.meetings.session.started'), { type: 'success' })
      invalidate()
    },
    onError: error => failureNotice(error, 'apps.meetings.session.startFailed'),
  })

  const statusMutation = useMutation({
    mutationFn: (next: MeetingStatus) => meetingsApi.setStatus(meetingId, next),
    onSuccess: () => invalidate(),
    onError: error => failureNotice(error, 'apps.meetings.session.statusFailed'),
  })

  const stopMutation = useMutation({
    mutationFn: () => meetingsApi.stop(meetingId),
    onSuccess: () => {
      notify(i18nT('apps.meetings.session.ended'), { type: 'info' })
      invalidate()
      void queryClient.invalidateQueries({ queryKey: ['meetings', 'list'] })
    },
    onError: error => failureNotice(error, 'apps.meetings.session.stopFailed'),
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
    onError: error => failureNotice(error, 'apps.meetings.session.agentToggleFailed'),
  })

  const broadcastMutation = useMutation({
    mutationFn: (text: string) => meetingsApi.dispatch(meetingId, text, true),
    onSuccess: () => notify(i18nT('apps.meetings.session.broadcastSent'), { type: 'info' }),
    onError: error => failureNotice(error, 'apps.meetings.session.broadcastFailed'),
  })

  const agentMessageMutation = useMutation({
    mutationFn: (vars: { agentId: string; text: string }) =>
      meetingsApi.message(meetingId, vars.agentId, vars.text),
    onSuccess: () => invalidate(),
    onError: error => failureNotice(error, 'apps.meetings.session.messageFailed'),
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
    onError: error => failureNotice(error, 'apps.meetings.session.attachmentFailed'),
  })

  // ── task mutations ────────────────────────────────────────────────────────

  const addTaskMutation = useMutation({
    mutationFn: (description: string) => meetingsApi.addTask(meetingId, description),
    onSuccess: () => invalidate(),
    onError: error => failureNotice(error, 'apps.meetings.session.taskAddFailed'),
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
    onError: error => failureNotice(error, 'apps.meetings.session.taskFileFailed'),
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
    caption,
    chatViewAgents,
    selectedPreset,
    transcription,
    loading: initQuery.isLoading || metaQuery.isLoading,
    error: (initQuery.error ?? metaQuery.error) as Error | null,
    agentsPaused: Boolean(live?.agents_paused),
    syncing: metaQuery.isFetching || outputsQuery.isFetching,
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
