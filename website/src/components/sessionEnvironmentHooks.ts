import { useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useRef } from 'react'
import { useTranslation } from 'react-i18next'
import { api, type SessionEnvironmentStartStage } from '../api/client'
import { fmtElapsed } from '../i18n/format'

/* The slot-environment query and the hooks other surfaces read it through.
 * Kept apart from SessionEnvironmentChip so the chat bundle can import these
 * without pulling the chip (and its menu) in eagerly: the
 * chip itself is lazy-loaded at its render site. */

/** Start-stage poll cadence; also what ticks the elapsed time on the chip. */
export const START_POLL_MS = 1000

/** Static keys (no template literal) so the i18n gates can see every one. */
function stageText(t: (key: string) => string, stage: SessionEnvironmentStartStage): string {
  switch (stage) {
    case 'checking': return t('sessionEnvironments.picker.stage.checking')
    case 'provisioning': return t('sessionEnvironments.picker.stage.provisioning')
    case 'connecting': return t('sessionEnvironments.picker.stage.connecting')
    case 'starting_agent': return t('sessionEnvironments.picker.stage.starting_agent')
    default: return t('sessionEnvironments.picker.stage.checking')
  }
}

export const slotEnvironmentQueryKey = (slot: string) => ['slot-environment', slot] as const

type RemoteEnvironment = { profile: string; name: string; workdir: string }

/** Only an explicit successful local placement permits gateway file/shell access.
 *  Undefined means unread, loading or failed, including a failed refresh of a
 *  cached local record. Shares the chip's query. */
export function useSlotEnvironmentPlacement(slot: string | null | undefined) {
  const { data, isSuccess, error } = useQuery({
    queryKey: slotEnvironmentQueryKey(slot ?? ''),
    queryFn: () => api.chatSlotEnvironment(slot ?? ''),
    enabled: !!slot,
    staleTime: 30_000,
    retry: false,
  })
  const id = data?.profile
  const match = id ? data?.profiles?.find((p) => p.id === id) : undefined
  const remote = useMemo<RemoteEnvironment | null | undefined>(() => {
    if (!slot || !isSuccess) return undefined
    if (id === null) return null
    if (typeof id !== 'string' || !id) return undefined
    return { profile: id, name: match?.name ?? id, workdir: match?.workdir ?? '' }
  }, [slot, isSuccess, id, match?.name, match?.workdir])
  return { remote, error }
}

/** Display-only remote label. Actions must use the placement hook to distinguish
 *  confirmed local from unknown. */
export function useSlotRemoteEnvironment(slot: string | null | undefined): RemoteEnvironment | null {
  return useSlotEnvironmentPlacement(slot).remote ?? null
}

/** "Starting session environment · 22 s" while ``slot``'s remote environment is starting
 *  during a running turn, else null. Shares the chip's query (same key), so the
 *  transcript's turn indicator and the chip read one poll, never two.
 *
 *  For the turn indicator: a first remote turn spends its opening minute(s)
 *  creating compute, and "Thinking…" there misdescribes what is happening. */
export function useEnvironmentStartLabel(
  slot: string | null | undefined,
  running: boolean,
  /** The session has replied before, so its environment connected at least once. */
  connectedBefore = false,
): EnvironmentStart | null {
  const { t } = useTranslation()
  const { data, dataUpdatedAt, refetch } = useQuery({
    queryKey: slotEnvironmentQueryKey(slot ?? ''),
    queryFn: () => api.chatSlotEnvironment(slot ?? ''),
    enabled: !!slot && running,
    staleTime: 30_000,
    retry: false,
    refetchInterval: (query) => (running && query.state.data?.profile ? START_POLL_MS : false),
  })
  // The cached record predates the turn (it says "not starting"), so without
  // an immediate read the first second of a start showed "Thinking…".
  const wasRunning = useRef(false)
  useEffect(() => {
    if (running && !wasRunning.current && slot) void refetch()
    wasRunning.current = running
  }, [running, slot, refetch])
  // When this turn began, for the gap before the backend reports its first
  // stage: the send, the turn queue and the session lookup all run before the
  // environment start does, and "Thinking…" there is wrong too.
  const turnStarted = useRef(0)
  if (running && !turnStarted.current) turnStarted.current = Date.now()
  if (!running) turnStarted.current = 0
  const remote = running && !!data?.profile
  // An active session's engine restart reconnects to a workspace that is
  // already up in a few seconds, and a setup checklist mid-conversation reads
  // as starting over. Only a restart that has to create or start compute again
  // (a stopped workspace) is worth the card; otherwise the turn shows as usual.
  // Two witnesses: the transcript (a reply means it connected before) and the
  // gateway (it already had this session attached). The composer chip passes
  // no transcript, so without the gateway's flag it alone kept saying
  // "Starting agent" while the transcript said "Thinking…".
  const reconnecting = (connectedBefore || !!data?.starting?.reconnect)
    && data?.starting?.stage !== 'provisioning'
    && !(data?.starting?.entered ?? []).includes('provisioning')
  const reported = remote && !reconnecting ? data?.starting ?? null : null
  // Not attached and nothing reported yet = setup has not begun: show it as
  // the first stage rather than falling through to "Thinking…".
  const pendingSetup = remote && !reconnecting && !reported && data?.attached === false
  const stageKey: SessionEnvironmentStartStage | null = reported?.stage ?? (pendingSetup ? 'checking' : null)
  const sincePoll = Math.max(0, Date.now() - dataUpdatedAt)
  // The ONE timer: how long the current step has run (shown on that step).
  const stageMs = reported
    ? reported.stage_elapsed_s * 1000 + sincePoll
    : pendingSetup ? Date.now() - turnStarted.current : 0
  const elapsed = stageKey ? fmtElapsed(stageMs) : ''
  const stage = stageKey ? stageText(t, stageKey) : ''
  const profileName = data?.profiles?.find((p) => p.id === data?.profile)?.name ?? data?.profile ?? ''
  const label = stageKey ? t('sessionEnvironments.picker.startingHeader', { name: profileName }) : ''
  // Stable identity between polls so the memoized footer does not re-render
  // on every unrelated parent render.
  return useMemo(() => {
    if (!stageKey || !label) return null
    const current = START_STAGE_ORDER.indexOf(stageKey)
    // Every stage before the current one is done, whether or not the gateway
    // had to run it: one it never entered still has its outcome (a running
    // workspace is already started), and a dash there read as a failure. A
    // stage can also finish between two polls, so polled sightings could not
    // tell "ran" from "not needed" anyway.
    const steps = START_STAGE_ORDER.map((key, i): EnvironmentStartStep => ({
      key,
      label: stageText(t, key),
      state: i === current ? 'current' : i > current ? 'pending' : 'done',
    }))
    return { label, stage, elapsed, steps }
  }, [label, stage, elapsed, stageKey, t])
}

const START_STAGE_ORDER: SessionEnvironmentStartStage[] = ['checking', 'provisioning', 'connecting', 'starting_agent']

export interface EnvironmentStartStep {
  key: SessionEnvironmentStartStage
  label: string
  state: 'done' | 'skipped' | 'current' | 'pending'
}

export interface EnvironmentStart {
  /** "Setting up Coder MicroVM (dev)": the heading, with no timer of its own. */
  label: string
  /** The current stage alone, for live regions (the label ticks every second). */
  stage: string
  elapsed: string
  steps: EnvironmentStartStep[]
}
