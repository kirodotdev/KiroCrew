import { useSyncExternalStore } from 'react'
import { api } from '../api/client'
import { prepareTerminalRetirement, cancelTerminalRetirement, readTerminalSessionLease, retireTerminalSession, normalizeTerminalScope, captureTerminalSessionLease, getBottomTerminalSnapshot, terminalStateLoaded, markTerminalRetirementPending, activateTerminalSession, type TerminalSessionLease, type TerminalSessionScope } from '../hooks/useBottomTerminal'

interface CleanupFailure {
  key: string
  phase: 'delete' | 'cleanup' | 'terminals'
  lease?: TerminalSessionLease
  /** null means canonical retirement still needs to commit. */
  ids: string[] | null
  busy: boolean
}
const identity = (key: string) => normalizeTerminalScope(key)!
const failures = new Map<string, CleanupFailure>()
const deleting = new Map<string, Promise<void>>()
const listeners = new Set<() => void>()
let snapshot: CleanupFailure[] = []
let resetGeneration = 0
function publish() { snapshot = [...failures.values()]; listeners.forEach(listener => listener()) }
function subscribe(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener) } }
export function useHistoryTerminalCleanupFailures() {
  return useSyncExternalStore(subscribe, () => snapshot, () => snapshot)
}

async function cleanup(key: string, lease: TerminalSessionLease, pendingIds: string[] | null, historyConfirmed = true): Promise<void> {
  const started = resetGeneration
  let ids = pendingIds
  try {
    ids ??= await retireTerminalSession(lease)
    if (started !== resetGeneration) return
    if (ids.length) {
      // CliPanel imports terminal state too; keep this edge lazy and load it
      // only when there are actual PTYs to dispose, after the canonical commit.
      const { disposeTerminalSession, deleteTerminalSessionRequest } = await import('../components/CliPanel')
      if (started !== resetGeneration) return
      ids.forEach(disposeTerminalSession)
      const results = await Promise.allSettled(ids.map(id => deleteTerminalSessionRequest(id, true)))
      if (started !== resetGeneration) return
      ids = ids.filter((_, index) => results[index].status === 'rejected')
      if (ids.length) throw new Error('terminal-cleanup-request-failed')
    }
    failures.delete(identity(key))
  } catch {
    if (started !== resetGeneration) return
    // The HTTP deletion already succeeded. Keep a visible, cleanup-only retry;
    // repeating the history DELETE would produce an ambiguous {ok:false}.
    failures.set(identity(key), { key, phase: historyConfirmed ? 'cleanup' : 'terminals', lease, ids, busy: false })
  }
  publish()
}

async function deleteConfirmedHistory(key: string): Promise<void> {
  const started = resetGeneration
  let previous = failures.get(identity(key))
  if (previous?.phase === 'cleanup') {
    const current = await readTerminalSessionLease(key)
    if (started !== resetGeneration) return
    if (current.epoch <= (previous.lease?.epoch ?? current.epoch) + 1) return retryHistoryTerminalCleanup(key)
    // A new valid conversation reused this key. The old confirmation cannot
    // stand in for deleting that new conversation. Finish only the old PTYs first.
    await retryHistoryTerminalCleanup(key)
    if (failures.has(identity(key))) throw new Error('terminal-cleanup-incomplete')
    previous = undefined
  }
  if (previous?.phase === 'terminals') {
    await retryHistoryTerminalCleanup(key)
    if (failures.has(identity(key))) throw new Error('terminal-cleanup-incomplete')
    previous = undefined
  }
  if (previous) {
    failures.set(identity(key), { ...previous, busy: true }); publish()
    if (previous.lease) {
      try { await cancelTerminalRetirement(previous.lease) } catch {
        failures.set(identity(key), { ...previous, busy: false }); publish(); throw new Error('terminal-retirement-cancel-failed')
      }
    }
  }
  if (started !== resetGeneration) return
  let lease: TerminalSessionLease | undefined
  try {
    lease = await prepareTerminalRetirement(key)
    if (started !== resetGeneration) return
    const result = await api.deleteSession(key)
    if (started !== resetGeneration) return
    if (result?.ok !== true) throw new Error('history-delete-not-confirmed')
    markTerminalRetirementPending(lease)
  } catch (error) {
    if (started !== resetGeneration) return
    if (lease) {
      try { await cancelTerminalRetirement(lease) } catch { /* Retry releases this preparation before repeating HTTP. */ }
    }
    failures.set(identity(key), { key, phase: 'delete', lease, ids: null, busy: false })
    publish()
    throw error
  }
  await cleanup(key, lease, null)
}
export function deleteHistoryWithTerminals(key: string): Promise<void> {
  const existing = deleting.get(identity(key))
  if (existing) return existing
  const pending = deleteConfirmedHistory(key).finally(() => {
    if (deleting.get(identity(key)) === pending) { deleting.delete(identity(key)); publish() }
  })
  deleting.set(identity(key), pending)
  return pending
}
export async function retryHistoryTerminalCleanup(key: string): Promise<void> {
  const failure = failures.get(identity(key))
  if (!failure || failure.busy || failure.phase === 'delete' || !failure.lease) return
  failures.set(identity(key), { ...failure, busy: true }); publish()
  await cleanup(key, failure.lease, failure.ids, failure.phase === 'cleanup')
}
export function historyDeletionInFlight(scope: TerminalSessionScope): boolean {
  return scope !== null && deleting.has(identity(scope))
}
/** Explicit terminal-only authorization, used when a prepared deletion has no
 * confirmed server result in this renderer. Never issue a history DELETE. */
export async function closePreparedTerminals(lease: TerminalSessionLease): Promise<void> {
  if (lease.scope === null || historyDeletionInFlight(lease.scope)) return
  const existing = failures.get(identity(lease.scope))
  if (existing?.busy) return
  if (existing && existing.phase !== 'delete') return retryHistoryTerminalCleanup(lease.scope)
  failures.set(identity(lease.scope), { key: lease.scope, phase: 'terminals', lease, ids: null, busy: true })
  publish()
  await cleanup(lease.scope, lease, null, false)
}
interface TerminalActivation {
  lease: TerminalSessionLease
  loaded: boolean
  retired: boolean
  confirmedDelete: boolean
  deletedEpoch?: number
}
/** Capture before the request which validates a chat, never after its response. */
export function captureTerminalActivation(key: string): TerminalActivation {
  return { lease: captureTerminalSessionLease(key), loaded: terminalStateLoaded(),
    retired: getBottomTerminalSnapshot(key).retired,
    confirmedDelete: failures.get(identity(key))?.phase === 'cleanup',
    deletedEpoch: failures.get(identity(key))?.lease?.epoch }
}
async function reviveValidated(key: string, activation: TerminalActivation, started: number): Promise<void> {
  if (started !== resetGeneration) return
  const current = captureTerminalSessionLease(key)
  if (activation.confirmedDelete) {
    const failure = failures.get(identity(key))
    if (failure?.phase === 'cleanup' && failure.lease?.epoch === activation.lease.epoch && failure.ids === null) {
      // Finish retiring the old generation before the new conversation can
      // allocate. Keep its returned IDs for cleanup-only retry after revival.
      try {
        const ids = await retireTerminalSession(failure.lease)
        if (started !== resetGeneration) return
        failures.set(identity(key), { ...failure, ids })
        publish()
      } catch { return }
    }
    const retired = captureTerminalSessionLease(key)
    if (getBottomTerminalSnapshot(key).retired && retired.epoch === (activation.deletedEpoch ?? activation.lease.epoch) + 1) {
      await activateTerminalSession(key, retired)
    }
    if (started === resetGeneration) void retryHistoryTerminalCleanup(key)
  } else if (current.epoch === activation.lease.epoch && activation.retired) {
    await activateTerminalSession(key, activation.lease)
  }
}
/** Chat navigation never awaits this terminal-only recovery. Cold or changed
 * leases require a fresh server lookup before revival, so a stale response
 * cannot revive a deletion that happened while that response was in flight. */
export async function activateTerminalsAfterNavigation(key: string, activation: TerminalActivation): Promise<void> {
  const started = resetGeneration
  try {
    if (!activation.loaded) await readTerminalSessionLease(key)
    if (started !== resetGeneration || !getBottomTerminalSnapshot(key).retired) return
    const current = captureTerminalActivation(key)
    if (activation.loaded && activation.retired && current.lease.epoch === activation.lease.epoch) {
      await reviveValidated(key, activation, started)
      return
    }
    // Only a retired scope reaches this request; normal navigation performs no
    // extra request and never waits for terminal storage.
    await api.chatSlotDetail(key, 1, 0)
    if (started === resetGeneration) await reviveValidated(key, current, started)
  } catch { /* The terminal initializer or cleanup notice owns storage failure. */ }
}
export function __resetHistoryTerminalCleanupForTests() { resetGeneration++; failures.clear(); deleting.clear(); publish() }
