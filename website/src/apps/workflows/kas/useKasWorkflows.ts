/**
 * Data hooks for KAS-native workflows: the engine probe that gates every KAS
 * surface, the cheap state poll, a project's recipes and run history, and the
 * run/approval mutations.
 */
import { useSyncExternalStore } from 'react'
import { useMutation, useQuery, useQueryClient, keepPreviousData } from '@tanstack/react-query'

import { api, KAS_EFFORT_LEVELS, type KasApproval, type KasQuestionAnswers, type KasRunAction, type KasRunRow, type KasRunsSnapshot } from '../../../api/client'
import { safeGetItem, safeSetItem } from '../../../utils/safeStorage'
import { isTerminalRun, liveStatusLine, needsAttention, runTitle } from './kasModel'

export const KAS_ENGINE_KEY = ['workflows', 'engine'] as const
export const KAS_STATE_KEY = ['workflows', 'kas', 'state'] as const
export const kasRecipesKey = (cwd: string) => ['workflows', 'kas', 'recipes', cwd] as const
export const kasRunsKey = (cwd: string) => ['workflows', 'kas', 'runs', cwd] as const

/** How often the state poll runs while the page is visible. */
export const KAS_POLL_MS = 2000

/**
 * True when the KAS surfaces should render: the gateway says KAS owns workflows.
 *
 * One gateway-wide opt-in now (`agent.kas_workflows`), folded into the engine
 * answer server-side, so `engine === 'kas'` already means "the backend is Kiro
 * Agent AND the operator opted in". There is no per-device preview flag to read
 * and no localStorage: every tab reads the same gateway answer (one cached
 * request, `staleTime` 60s). A failed probe reads as "not KAS": the Crew
 * surfaces stay exactly as they are.
 */
export function useKasWorkflowsEnabled(): { enabled: boolean; loading: boolean } {
  const engine = useQuery({
    queryKey: KAS_ENGINE_KEY,
    queryFn: () => api.workflowEngine(),
    staleTime: 60_000,
    retry: false,
  })
  return {
    enabled: engine.data?.engine === 'kas',
    loading: engine.isLoading,
  }
}

/**
 * Keep the previous snapshot object when the revision has not moved, so a
 * 2-second poll that brings nothing new re-renders nothing.
 */
export function keepSameRevision(prev: unknown, next: unknown): unknown {
  const a = prev as KasRunsSnapshot | undefined
  const b = next as KasRunsSnapshot | undefined
  if (a && b && typeof a.revision === 'number' && a.revision === b.revision) return a
  return next
}

/** The router's live runs and approvals, polled every ~2s while visible. */
export function useKasState(enabled: boolean) {
  return useQuery({
    queryKey: KAS_STATE_KEY,
    queryFn: () => api.kasState(),
    enabled,
    refetchInterval: KAS_POLL_MS,
    // A hidden tab stops polling; React Query's default, stated so a reader
    // does not have to know it.
    refetchIntervalInBackground: false,
    structuralSharing: keepSameRevision,
  })
}

/** The recipes KAS can launch in `cwd`. */
export function useKasRecipes(cwd: string, enabled: boolean) {
  return useQuery({
    queryKey: kasRecipesKey(cwd),
    queryFn: () => api.kasRecipes(cwd),
    enabled: enabled && !!cwd,
    staleTime: 30_000,
  })
}

/**
 * The project's run history from disk. Re-read whenever the live state learns
 * of a run it did not know (`knownIds` changes), so a run launched from here
 * joins the project list without a per-poll disk scan.
 */
export function useKasProjectRuns(cwd: string, enabled: boolean, knownIds: string) {
  return useQuery({
    queryKey: [...kasRunsKey(cwd), knownIds],
    queryFn: () => api.kasRuns(cwd),
    enabled: enabled && !!cwd,
    placeholderData: keepPreviousData,
  })
}

/** Run controls, approval answers and reconnect, each refreshing the state poll. */
export function useKasRunActions() {
  const queryClient = useQueryClient()
  const refresh = () => queryClient.invalidateQueries({ queryKey: ['workflows', 'kas'] })
  const control = useMutation({
    mutationFn: ({ workflowId, action }: { workflowId: string; action: KasRunAction }) =>
      api.kasRunControl(workflowId, action),
    onSettled: refresh,
  })
  const answer = useMutation({
    mutationFn: ({ key, optionId }: { key: string; optionId: string }) => api.kasAnswerApproval(key, optionId),
    onSettled: refresh,
  })
  const reconnect = useMutation({
    mutationFn: (slot: string) => api.kasReconnectSlot(slot),
    onSettled: refresh,
  })
  return { control, answer, reconnect }
}

/** Retry / Run again, and the per-step writes (grant, mark done/failed, stop one step). */
export function useKasParityActions() {
  const queryClient = useQueryClient()
  const refresh = () => queryClient.invalidateQueries({ queryKey: ['workflows', 'kas'] })
  const retry = useMutation({
    mutationFn: ({ workflowId, nodeId, path }: { workflowId: string; nodeId?: string; path?: string | null }) =>
      api.kasRetryRun(workflowId, nodeId ? { nodeId, path } : undefined),
    onSettled: refresh,
  })
  const restart = useMutation({
    mutationFn: ({ workflowId }: { workflowId: string }) => api.kasRestartRun(workflowId),
    onSettled: refresh,
  })
  const step = useMutation({
    mutationFn: ({
      workflowId,
      nodeId,
      verb,
      path,
      iterations,
      status,
      reason,
    }: {
      workflowId: string
      nodeId: string
      verb: 'grant' | 'complete' | 'cancel'
      path?: string | null
      iterations?: number
      status?: 'completed' | 'failed'
      reason?: string
    }) =>
      api.kasStepAction(workflowId, nodeId, verb, {
        path,
        ...(iterations != null ? { iterations } : {}),
        ...(status ? { status } : {}),
        ...(reason ? { reason } : {}),
      }),
    onSettled: refresh,
  })
  const question = useMutation({
    mutationFn: ({ key, answers, dismiss }: { key: string; answers?: KasQuestionAnswers; dismiss?: boolean }) =>
      api.kasAnswerQuestion(key, dismiss ? { dismiss: true } : { answers: answers ?? {} }),
    onSettled: refresh,
  })
  return { retry, restart, step, question }
}

// ── Workflow dock layout: a device-local display preference ──

/** How several live workflows in one chat collapse: stacked cards, or one combined card. */
export type KasDockMode = 'stack' | 'combined'
export const KAS_DOCK_MODE_KEY = 'mc-kas-workflow-dock'
const KAS_DOCK_MODE_EVENT = 'mc-kas-workflow-dock-changed'

export function readKasDockMode(): KasDockMode {
  return safeGetItem(KAS_DOCK_MODE_KEY) === 'combined' ? 'combined' : 'stack'
}

export function writeKasDockMode(mode: KasDockMode): void {
  safeSetItem(KAS_DOCK_MODE_KEY, mode)
  window.dispatchEvent(new Event(KAS_DOCK_MODE_EVENT))
}

const subscribeDockMode = (cb: () => void) => {
  const onStorage = (e: StorageEvent) => {
    if (e.key === KAS_DOCK_MODE_KEY) cb()
  }
  window.addEventListener(KAS_DOCK_MODE_EVENT, cb)
  window.addEventListener('storage', onStorage)
  return () => {
    window.removeEventListener(KAS_DOCK_MODE_EVENT, cb)
    window.removeEventListener('storage', onStorage)
  }
}

/** The dock layout, live across the Settings switch and other tabs. */
export function useKasDockMode(): [KasDockMode, (mode: KasDockMode) => void] {
  const mode = useSyncExternalStore(subscribeDockMode, readKasDockMode, () => 'stack' as KasDockMode)
  return [mode, writeKasDockMode]
}

/** The gateway setting for the effort of a prompt-created workflow's creator step. */
export const KAS_CREATOR_EFFORT_CONFIG_KEY = 'agent.kas_creator_effort'

function creatorEffortOf(config: unknown): string {
  const agent = config && typeof config === 'object' ? (config as { agent?: unknown }).agent : null
  const value = agent && typeof agent === 'object' ? (agent as Record<string, unknown>).kas_creator_effort : null
  return typeof value === 'string' && (KAS_EFFORT_LEVELS as readonly string[]).includes(value) ? value : ''
}

/** Read/write `agent.kas_creator_effort` ('' = Kiro Agent's own default). */
export function useKasCreatorEffort(enabled: boolean) {
  const queryClient = useQueryClient()
  const config = useQuery({ queryKey: ['kirocrewConfig'], queryFn: () => api.kirocrewConfig(), enabled })
  const save = useMutation({
    // '' is "Kiro Agent default", as the setting defines it.
    mutationFn: (value: string) => api.patchConfig(KAS_CREATOR_EFFORT_CONFIG_KEY, value),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
  })
  const stored = creatorEffortOf(config.data)
  return { value: save.isPending ? (save.variables ?? '') : stored, loaded: config.isSuccess, save }
}

/** The one gateway setting that opts into Kiro Agent's native workflows. */
export const KAS_WORKFLOWS_CONFIG_KEY = 'agent.kas_workflows'

/**
 * The single opt-in's state and a setter, read from the engine probe.
 *
 * `on` is the stored `agent.kas_workflows` value (reflected even off a Kiro
 * Agent backend, so the toggle shows what is saved); `kasBackend` is whether the
 * backend could serve KAS workflows at all, so the UI can say the switch has no
 * effect until the backend is Kiro Agent. Written through the ordinary config
 * PATCH, which re-reads the probe after.
 */
export function useKasWorkflowsSetting() {
  const queryClient = useQueryClient()
  const engine = useQuery({
    queryKey: KAS_ENGINE_KEY,
    queryFn: () => api.workflowEngine(),
    staleTime: 60_000,
    retry: false,
  })
  const save = useMutation({
    mutationFn: (on: boolean) => api.patchConfig(KAS_WORKFLOWS_CONFIG_KEY, on),
    onSettled: () => queryClient.invalidateQueries({ queryKey: KAS_ENGINE_KEY }),
  })
  return {
    on: save.isPending ? !!save.variables : !!engine.data?.kas_workflows,
    kasBackend: !!engine.data?.kas_backend,
    loaded: engine.isSuccess,
    save,
  }
}

export const kasStepTranscriptKey = (workflowId: string, nodeId: string, path?: string | null) =>
  ['workflows', 'kas', 'transcript', workflowId, nodeId, path ?? ''] as const

/**
 * One step's own chat. Polled every ~2s while the step (or its session) is
 * running, read once otherwise. A 404 (`step_unknown`) is an answer, not a
 * failure, so it is not retried. `path` names one loop iteration when the step
 * is inside one; different iterations are cached (and fetched) apart.
 */
export function useKasStepTranscript(
  workflowId: string,
  nodeId: string | null,
  stepRunning: boolean,
  path?: string | null,
) {
  return useQuery({
    queryKey: kasStepTranscriptKey(workflowId, nodeId ?? '', path),
    queryFn: () => api.kasStepTranscript(workflowId, nodeId as string, path),
    enabled: !!nodeId,
    retry: false,
    refetchInterval: query => (stepRunning || query.state.data?.status === 'running' ? KAS_POLL_MS : false),
    refetchIntervalInBackground: false,
  })
}

/**
 * Send the person's reply to a step parked for their next message. On success
 * the step's transcript is refetched, so the reply (and the step's resumed
 * turn) appear without waiting for the next poll.
 */
export function useKasReplyToStep(workflowId: string, nodeId: string | null, path?: string | null) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (text: string) => api.kasReplyToStep(workflowId, nodeId as string, text, path),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: kasStepTranscriptKey(workflowId, nodeId ?? '', path) }),
  })
}

/**
 * One chat's runs as the gateway's run ledger knows them: the live poll only
 * holds runs that have spoken since the last gateway restart, so a chat reads
 * its own back once (read-only on the server) and merges them under the poll.
 */
export function useKasChatRuns(slotKey: string | null | undefined, enabled: boolean) {
  return useQuery({
    queryKey: ['workflows', 'kas', 'chat', slotKey ?? ''] as const,
    queryFn: () => api.kasChatRuns(slotKey as string),
    enabled: enabled && !!slotKey,
    staleTime: 60_000,
    retry: false,
  })
}

/** The live poll's rows, plus any ledger rows it does not hold (the poll wins). */
export function mergeRuns(live: readonly KasRunRow[], ledger: readonly KasRunRow[] | undefined): KasRunRow[] {
  if (!ledger || ledger.length === 0) return [...live]
  const known = new Set(live.map(r => r.workflowId))
  return [...live, ...ledger.filter(r => !known.has(r.workflowId))]
}

/** What a session-sidebar row shows for its chat's live KAS runs. */
export interface KasSidebarActivity {
  count: number
  name: string
  phase: string
  attention: boolean
}

/** The chat's unfinished runs, summarized for one sidebar row; undefined when none. */
export function summarizeSidebarActivity(runs: readonly KasRunRow[], slotKey: string): KasSidebarActivity | undefined {
  const live = runs.filter(r => r.slotKey === slotKey && !isTerminalRun(r))
  if (live.length === 0) return undefined
  const first = live.find(needsAttention) ?? live[0]
  const line = liveStatusLine(first)
  return {
    count: live.length,
    name: runTitle(first),
    phase: line?.text ?? '',
    attention: live.some(needsAttention),
  }
}

/**
 * One session row's KAS run activity, from the shared state poll. `select`
 * narrows the snapshot to this row's primitives, so a poll that changes some
 * other chat's run re-renders no row but that chat's (the sidebar's per-row
 * render budget). Inert, with no request, until the preview is on and the
 * gateway answers `kas`.
 */
export function useKasSidebarActivity(slotKey: string): KasSidebarActivity | undefined {
  const { enabled } = useKasWorkflowsEnabled()
  const { data } = useQuery({
    queryKey: KAS_STATE_KEY,
    queryFn: () => api.kasState(),
    enabled: enabled && !!slotKey,
    refetchInterval: KAS_POLL_MS,
    refetchIntervalInBackground: false,
    structuralSharing: keepSameRevision,
    select: (snap: KasRunsSnapshot) => summarizeSidebarActivity(snap.runs ?? [], slotKey),
  })
  return enabled ? data : undefined
}

/**
 * The held approvals (step tool approvals and plan approvals) of the runs that
 * report into ONE chat slot. An approval is tied to its run by `workflowId`;
 * the snapshot's runs carry the `slotKey`, so this joins the two. Pure, so the
 * `select` below and the composer card are both tested without a client.
 */
export function approvalsForSlot(snap: KasRunsSnapshot, slotKey: string): KasApproval[] {
  const ids = new Set((snap.runs ?? []).filter(r => r.slotKey === slotKey).map(r => r.workflowId))
  return (snap.approvals ?? []).filter(a => ids.has(a.workflowId))
}

/**
 * Gap 9: this chat's held KAS approvals, for a nested card ABOVE the composer,
 * so a step waiting on a person can be answered without hunting for the run
 * card (which scrolls away). Uses the ONE shared state poll (react-query dedupes
 * on `KAS_STATE_KEY`), narrowed to this slot's runs, plus the same answer
 * mutation the run cards use. Inert — no request, no approvals — unless the KAS
 * preview is on and this chat has a slot key.
 */
export function useKasComposerApprovals(slotKey: string | null | undefined): {
  approvals: KasApproval[]
  answer: ReturnType<typeof useKasRunActions>['answer']
} {
  const { enabled } = useKasWorkflowsEnabled()
  const { answer } = useKasRunActions()
  const { data } = useQuery({
    queryKey: KAS_STATE_KEY,
    queryFn: () => api.kasState(),
    enabled: enabled && !!slotKey,
    refetchInterval: KAS_POLL_MS,
    refetchIntervalInBackground: false,
    structuralSharing: keepSameRevision,
    select: (snap: KasRunsSnapshot) => approvalsForSlot(snap, slotKey as string),
  })
  return { approvals: enabled ? (data ?? []) : [], answer }
}
