import { useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useRef } from 'react'
import { useTranslation } from 'react-i18next'
import { api, type SessionEnvironmentStartStage } from '../api/client'
import { ApiError, apiErrorCode } from '../api/apiError'
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

/** Every reader of the shared query retries alike: whichever mounts first sets
 *  the fetch's options, and one failed read must not replace a local chat's Files. */
export const SLOT_ENVIRONMENT_RETRY = { retry: 2, retryDelay: 250 } as const

type RemoteEnvironment = { profile: string; name: string; workdir: string }

/** Only an explicit successful local placement (or the read's `owner_only`
 *  refusal: a caller that cannot bind chats) permits gateway file/shell access.
 *  Undefined means unread, loading or failed, including a failed refresh of a
 *  cached local record. Shares the chip's query. */
export function useSlotEnvironmentPlacement(slot: string | null | undefined) {
  const { data, isSuccess, error } = useQuery({
    queryKey: slotEnvironmentQueryKey(slot ?? ''),
    queryFn: () => api.chatSlotEnvironment(slot ?? ''),
    enabled: !!slot,
    staleTime: 30_000,
    // A chat bound from another tab must not keep reading as local here.
    refetchOnWindowFocus: 'always',
    ...SLOT_ENVIRONMENT_RETRY,
  })
  const id = data?.profile
  const match = id ? data?.profiles?.find((p) => p.id === id) : undefined
  // A caller the read refuses as not the owner cannot bind chats, so for it every
  // chat runs here, as on main; a retry would meet the same refusal.
  const ownerOnly = error instanceof ApiError && error.status === 403 && apiErrorCode(error) === 'owner_only'
  const remote = useMemo<RemoteEnvironment | null | undefined>(() => {
    // No chat (the active one was just closed): nothing can be remote.
    if (!slot) return null
    if (ownerOnly) return null
    if (!isSuccess) return undefined
    if (id === null) return null
    if (typeof id !== 'string' || !id) return undefined
    return { profile: id, name: match?.name ?? id, workdir: match?.workdir ?? '' }
  }, [slot, ownerOnly, isSuccess, id, match?.name, match?.workdir])
  return { remote, error: ownerOnly ? null : error }
}

/** For a hand-off that runs a command on this machine for ``slot``'s chat (a
 *  `!` command, Run in terminal): true only once the chat is confirmed to run
 *  here, false when it runs on a profile, undefined while unread or failed. A
 *  caller the read refuses as not the owner cannot bind chats, so for it every
 *  chat runs here, as on main. */
export function useSlotRunsHere(slot: string | null | undefined): boolean | undefined {
  const { remote } = useSlotEnvironmentPlacement(slot)
  if (remote === null) return true
  if (remote) return false
  return undefined
}

/** Display-only remote label. Actions must use the placement hook to distinguish
 *  confirmed local from unknown. */
export function useSlotRemoteEnvironment(slot: string | null | undefined): RemoteEnvironment | null {
  return useSlotEnvironmentPlacement(slot).remote ?? null
}

/** "Starting session environment · 22 s" while ``slot``'s remote environment is starting
 *  during a running turn, else null. Shares the chip's query, so the turn
 *  indicator and the chip read one poll. */
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
    ...SLOT_ENVIRONMENT_RETRY,
    refetchInterval: (query) => (running && query.state.data?.profile ? START_POLL_MS : false),
  })
  // The cached record predates the turn: read at once when a turn starts.
  const wasRunning = useRef(false)
  useEffect(() => {
    if (running && !wasRunning.current && slot) void refetch()
    wasRunning.current = running
  }, [running, slot, refetch])
  // When this turn began, for the gap before the first reported stage.
  const turnStarted = useRef(0)
  if (running && !turnStarted.current) turnStarted.current = Date.now()
  if (!running) turnStarted.current = 0
  const remote = running && !!data?.profile
  // A reconnect (a prior reply, or the gateway's flag for the chip, which has
  // no transcript) shows as a normal turn unless it has to provision compute.
  const reconnecting = (connectedBefore || !!data?.starting?.reconnect)
    && data?.starting?.stage !== 'provisioning'
    && !(data?.starting?.entered ?? []).includes('provisioning')
  const reported = remote && !reconnecting ? data?.starting ?? null : null
  // Not attached and nothing reported yet: show the first stage.
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
  // Stable identity between polls for the memoized footer.
  return useMemo(() => {
    if (!stageKey || !label) return null
    const current = START_STAGE_ORDER.indexOf(stageKey)
    // Every earlier stage is done, entered or not: a skipped one still has its
    // outcome (a running workspace is started).
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
