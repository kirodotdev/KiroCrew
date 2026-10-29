/**
 * The Shared Crew side of the dashboard socket: the server frames folded into the
 * React Query cache, the reconnect rule, and the one client frame (activity).
 *
 * Frames are whole values, so a frame replaces what it names, keyed by id and
 * revision: a replay after a reconnect, or an HTTP read that lands after a newer
 * frame, can never move an item backwards or show it twice. On every (re)open the
 * HTTP reads are invalidated and refetched (the typing lease among them, so a
 * reconnecting tab learns who is typing), and the next frames are authoritative.
 * Nothing here ever resubmits a send.
 */
import type { QueryClient } from '@tanstack/react-query'
import { noteSendRevision, sendStateQueryKey } from '../../api/sendRace'
import type { Activity, MemoryChange, SendState, SharedCrewActivityFrame, SharedCrewServerFrame, TypingLease } from '../../api/sharedCrew'
import { mergeDecision, mergeWork, type DecisionsResponse, type PresenceResponse, type WorkListResponse } from '../../api/sharedCrewUi'

export const sharedCrewKeys = {
  all: ['shared-crew'] as const,
  capabilities: ['shared-crew', 'capabilities'] as const,
  capacity: ['shared-crew', 'capacity'] as const,
  people: ['shared-crew', 'people'] as const,
  mode: ['shared-crew', 'mode'] as const,
  viewer: ['shared-crew', 'viewer'] as const,
  sessionsAll: ['shared-crew', 'sessions'] as const,
  sessions: (creator: string, q: string) => ['shared-crew', 'sessions', creator, q] as const,
  work: (sessionKey: string) => ['shared-crew', 'work', sessionKey] as const,
  presence: (sessionKey: string) => ['shared-crew', 'presence', sessionKey] as const,
  decisions: (sessionKey: string) => ['shared-crew', 'decisions', sessionKey] as const,
  sendState: sendStateQueryKey,
  typingLease: (sessionKey: string) => ['shared-crew', 'typing-lease', sessionKey] as const,
  memoryChanges: ['shared-crew', 'memory-changes'] as const,
}

/** How many recent memory changes the cache keeps, newest first. */
export const MEMORY_CHANGES_KEPT = 20

/** Keep the cache at its newest revision without changing a snapshot's own answer. */
export function latestSendState(previous: unknown, next: unknown): SendState | null {
  const cached = previous as SendState | null | undefined
  const incoming = next as SendState | null
  return cached && (!incoming || cached.revision > incoming.revision) ? cached : incoming
}

/** A live frame supersedes any snapshot already in flight. Partial frames keep
 *  missing or invalidated snapshots stale until an active reader fetches the full list. */
function setFrameData<T>(queryClient: QueryClient, queryKey: readonly unknown[], value: T | ((previous: T | undefined) => T), partial = false): void {
  const state = queryClient.getQueryState(queryKey)
  const needsSnapshot = partial && (state?.data == null || state.isInvalidated || state.fetchStatus === 'fetching')
  void queryClient.cancelQueries({ queryKey, exact: true })
  queryClient.setQueryData<T>(queryKey, value)
  if (needsSnapshot) void queryClient.invalidateQueries({ queryKey, exact: true })
}

/** Fold one server frame into the cache. `data` is the frame the gateway sent. */
export function applySharedCrewFrame(queryClient: QueryClient, data: unknown): void {
  if (!data || typeof data !== 'object') return
  const frame = data as SharedCrewServerFrame
  switch (frame.type) {
    case 'shared_crew.presence': {
      const value: PresenceResponse = { type: frame.type, schema_version: frame.schema_version, session_key: frame.session_key, entries: frame.entries ?? [] }
      setFrameData(queryClient, sharedCrewKeys.presence(frame.session_key), value)
      void queryClient.invalidateQueries({ queryKey: sharedCrewKeys.people })
      return
    }
    case 'shared_crew.work': {
      const work = frame.work
      if (!work?.work_id) return
      void queryClient.invalidateQueries({ queryKey: sharedCrewKeys.people })
      // The chat reads its work list by tab key; the record names its session.
      const keys = new Set([work.session_key, frame.slot_key].filter((k): k is string => !!k))
      for (const key of keys) {
        setFrameData<WorkListResponse | null>(queryClient, sharedCrewKeys.work(key), prev => ({
          ...(prev ?? {}),
          items: mergeWork(prev?.items ?? [], work),
        }), true)
      }
      return
    }
    case 'shared_crew.decision': {
      const decision = frame.decision
      if (!decision?.decision_id) return
      setFrameData<DecisionsResponse | null>(queryClient, sharedCrewKeys.decisions(decision.session_key), prev => ({
        schema_version: prev?.schema_version ?? frame.schema_version,
        items: mergeDecision(prev?.items ?? [], decision),
      }), true)
      return
    }
    case 'shared_crew.capacity':
      setFrameData(queryClient, sharedCrewKeys.capacity, frame.capacity)
      return
    case 'shared_crew.send_state': {
      if (!frame.session_key) return
      noteSendRevision(frame.session_key, frame.revision)
      const value: SendState = { schema_version: frame.schema_version, session_key: frame.session_key, revision: frame.revision, last: frame.last ?? null }
      // A send pressed during this read needs its original answer. Let that
      // read finish; structural sharing keeps its older snapshot out of the cache.
      queryClient.setQueryData<SendState | null>(sharedCrewKeys.sendState(frame.session_key), prev => latestSendState(prev, value))
      return
    }
    case 'shared_crew.typing_lease': {
      if (!frame.session_key) return
      setFrameData<TypingLease | null>(queryClient, sharedCrewKeys.typingLease(frame.session_key), typingLeaseOf(frame))
      return
    }
    case 'shared_crew.memory_change': {
      const change = frame.change
      if (!change || typeof change.new !== 'string') return
      setFrameData<MemoryChange[]>(queryClient, sharedCrewKeys.memoryChanges, prev =>
        [change, ...(prev ?? [])].slice(0, MEMORY_CHANGES_KEPT))
      return
    }
  }
}

/** A lease value from a frame or a refusal body: whole-value, so it replaces. */
export function typingLeaseOf(raw: TypingLease): TypingLease {
  return {
    schema_version: raw.schema_version,
    session_key: raw.session_key,
    holder: raw.holder ?? null,
    holder_ids: Array.isArray(raw.holder_ids) ? raw.holder_ids : [],
    expires_at: raw.expires_at ?? null,
    idle_seconds: raw.idle_seconds,
    ended: raw.ended ?? '',
  }
}

/** A (re)opened socket: read server truth again; frames after this are current. */
export function onSharedCrewSocketOpen(queryClient: QueryClient): void {
  void queryClient.invalidateQueries({ queryKey: sharedCrewKeys.all }, { cancelRefetch: false })
}

type Sender = (payload: object) => void
let sender: Sender | null = null

/** Installed by the socket owner; `null` while there is no socket. */
export function setSharedCrewSender(next: Sender | null): void {
  sender = next
}

/**
 * Report this tab's activity in one session. The frame names no principal and
 * carries no text: the server stamps the connection's verified actor.
 */
export function sendActivity(sessionKey: string, activity: Activity, { draftEmpty = false }: { draftEmpty?: boolean } = {}): boolean {
  if (!sender || !sessionKey) return false
  const frame: SharedCrewActivityFrame = { type: 'shared_crew.activity', session_key: sessionKey, activity }
  if (draftEmpty) frame.draft = 'empty'
  sender(frame)
  return true
}
