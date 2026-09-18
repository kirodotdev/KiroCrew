import { type ReactNode, useEffect, useId, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Goal, Radar, X } from 'lucide-react'
import { Popover, PopoverTrigger, PopoverContent } from './ui/popover'
import { Btn } from './ui'
import ErrorNotice from './ErrorNotice'
import { cronJobsQuery } from '../api/cronJobsQuery'
import { runBelongsToSlot } from '../apps/workflows/runModel'
import {
  GOAL_DRAFT_MAX_CYCLES,
  GOAL_DRAFT_MAX_IDLE_SECS,
  GOAL_DRAFT_MAX_MESSAGE_CHARS,
  GOAL_DRAFT_MIN_IDLE_SECS,
  cacheCanonicalGoalDraft,
  clampGoalDraftMessage,
  enqueueRemoteGoalDraft,
  goalDraftMessageLength,
  isGoalDraftStorageKey,
  latestLocalGoalDraft,
  loadRemoteGoalDraft,
  sameGoalDraft,
  sameGoalDraftSnapshot,
  savePendingGoalDraft,
  saveRemoteGoalDraft,
  syncAcceptedCrossTabGoalDraft,
  type GoalDraft,
  type GoalDraftPersistResult,
  type GoalDraftSnapshot,
} from '../utils/goalDrafts'
import { DRAFT_SAVE_DEBOUNCE_MS } from '../utils/draftConstants'

import { i18nT } from '../i18n/t'
import { fmtTimeNumeric } from '../i18n/format'
import { type AutoNudgeLoop, cycleText as loopCycleText, nextCycleText, AUTONUDGE_LOOPS_QUERY_KEY } from './autoNudgeLoop'
export type { AutoNudgeLoop } from './autoNudgeLoop'

interface Props {
  slotKey: string
  loop: AutoNudgeLoop | null
  open: boolean
  onOpenChange: (open: boolean) => void
  onChange: (loop: AutoNudgeLoop | null) => void
  /** Present when this editor is the popover's default view and a bounded monitor can still be armed. */
  onSetUpBoundedMonitor?: () => void
  /** Disable legacy-loop writes while leaving Stop available for stale state. Also renders the reason. */
  writeDisabled?: boolean
  /**
   * True when the slot's last turn ended interrupted (the composer is showing
   * Resume). The chip stops pulsing and turns warn-coloured: the loop is still
   * armed, but nothing is running until the user resumes or the next idle-timer
   * cycle fires, and a pulsing chip would claim active work for that whole gap.
   */
  interrupted?: boolean
  /** Shared composer trigger supplied by the structured-monitor compatibility shell. */
  trigger?: ReactNode
  /** Structured body supplied by that shell; omitted to render the legacy editor. */
  content?: ReactNode
}

/**
 * The kill-switch placeholder the server substitutes at FIRE time
 * (`render_nudge_message` in `dashboard/handlers/autonudge.py` replaces it with
 * the loop's `stop_sentinel_path`). It must travel to `/api/autonudge`
 * verbatim -- substituting it in the form would leave the server nothing to
 * replace -- so the textarea keeps the raw token and the help line under it
 * explains what the token becomes (#10458). `DEFAULT_MSG` below ends with this
 * exact spelling; a test pins that the template still carries it.
 */
export const STOP_FILE_TOKEN = '{{STOP_FILE}}'

const DEFAULT_MSG = `Your north star is in north_star.md, roadmap in roadmap.md, tasks in tasks.md. Pick the single highest-leverage next step toward the goal and execute it. Update tasks.md. Post a blocker ONCE if genuinely stuck. To halt the loop, create {{STOP_FILE}}`

/** One armed script cron owned by this chat slot. */
interface SlotWatch {
  id: string
  name: string
  schedule: string
  next_run_ts: number | null
}

/**
 * `failed` and `clear-failed` are the same recovery (the snapshot is retained
 * and Retry reruns reconciliation) with different copy: one says a draft is
 * still held here, the other that a clear did not finish. The copy must follow
 * WHAT failed to sync, or a failed tombstone would tell the user a draft exists
 * while the editor shows the cleared template.
 */
type DraftSyncState = 'idle' | 'checking' | 'updated' | 'failed' | 'clear-failed'
type DraftPersistResult = GoalDraftPersistResult

/** A timestamped null draft is a clear tombstone; `updatedAt === 0` is no record at all. */
function syncFailureFor(snapshot: GoalDraftSnapshot): DraftSyncState {
  return snapshot.draft === null && snapshot.updatedAt > 0 ? 'clear-failed' : 'failed'
}

export default function AutoNudgePopover({ slotKey, loop, open, onOpenChange, onChange, onSetUpBoundedMonitor, writeDisabled = false, interrupted = false, trigger, content }: Props) {
  const messageLimitId = useId()
  // `||` (not `??`) is deliberate on the loop tier: it preserves the fallback
  // so a loop with idle_secs/max_cycles of 0 or an empty message still shows
  // the 60 / 0 / default template rather than a bare 0 / "".
  const [message, setMessage] = useState(() => clampGoalDraftMessage(loop?.message || DEFAULT_MSG))
  // Idle-seconds and max-cycles are held as RAW STRINGS while the popover is
  // open so every edit (including a fully-cleared field or a transient "") is
  // allowed as-typed. Coercing to a number on each keystroke would snap a
  // backspaced-to-empty field straight back to its default and prevent removing
  // the leading digit. The string is parsed
  // into a number only when the field commits (blur / save); an empty or
  // unparseable value falls back to the field default — 60 idle, 0 cycles.
  const [idleInput, setIdleInput] = useState(() => String(loop?.idle_secs || 60))
  const [maxCyclesInput, setMaxCyclesInput] = useState(() => String(loop?.max_cycles || 0))
  const messageLength = goalDraftMessageLength(message)
  const [saving, setSaving] = useState(false)
  /* Two-step on the clear only. The erase is irreversible and sits beside the
     primary CTA, so one press asks and the second performs. */
  const [confirmClear, setConfirmClear] = useState(false)
  const [error, setError] = useState('')
  const [draftSyncState, setDraftSyncState] = useState<DraftSyncState>('idle')
  const [draftSyncAttempt, setDraftSyncAttempt] = useState(0)
  // Watches armed on this slot, read through the SHARED `cron-jobs` query rather
  // than a private fetch. That key is invalidated by the websocket hook, so a
  // watch deleted or paused elsewhere disappears from an open popover instead of
  // lingering until it is reopened -- and the request dedupes with the other
  // consumer of the same key. `enabled: open` keeps a zero-token watch from
  // costing a request on every chat render just to say "still nothing".
  const queryClient = useQueryClient()
  const { data: cronJobs, isError: watchesFailed, refetch: refetchWatches } = useQuery({
    ...cronJobsQuery,
    enabled: open && content === undefined,
  })
  const { refetch: refetchRemoteDraft } = useQuery({
    queryKey: ['goal-draft', slotKey],
    queryFn: () => loadRemoteGoalDraft(slotKey),
    enabled: false,
    retry: false,
  })
  const remoteDraftMutation = useMutation({
    mutationFn: ({ targetSlot, snapshot, options }: {
      targetSlot: string
      snapshot: GoalDraftSnapshot
      options: { keepalive?: boolean; migration?: boolean }
    }) => saveRemoteGoalDraft(targetSlot, snapshot, options),
    onSuccess: (canonical, { targetSlot }) => {
      queryClient.setQueryData(['goal-draft', targetSlot], canonical)
    },
  })

  const watches: SlotWatch[] = useMemo(() => {
    const rows: unknown[] = Array.isArray(cronJobs) ? cronJobs : []
    return rows
      .filter((j): j is Record<string, unknown> => !!j && typeof j === 'object')
      .filter(j => {
        // One ownership rule, one spelling. `runBelongsToSlot` already maps a
        // session_key onto a chat slot against the same backend convention
        // (`dashboard:<slotKey>`); a second inline predicate here would drift
        // from it the day that key format moves.
        if (!runBelongsToSlot(typeof j.session_key === 'string' ? j.session_key : '', slotKey)) {
          return false
        }
        // A watch is a SCRIPT cron: it runs a Python callable and never reaches a
        // model. A message-only cron on this slot is an ordinary reminder that
        // DOES wake the agent, so it does not belong under a heading that
        // promises zero tokens.
        return typeof j.script === 'string' && !!j.script && j.enabled !== false
      })
      .map(j => ({
        id: String(j.id ?? ''),
        name: String(j.name ?? ''),
        schedule: String(j.schedule ?? ''),
        next_run_ts: typeof j.next_run_ts === 'number' ? j.next_run_ts : null,
      }))
  }, [cronJobs, slotKey])

  const clampInteger = (value: number, minimum: number, maximum: number) =>
    Math.min(maximum, Math.max(minimum, value))
  const parseIdle = (s: string) => clampInteger(
    parseInt(s, 10) || 60,
    GOAL_DRAFT_MIN_IDLE_SECS,
    GOAL_DRAFT_MAX_IDLE_SECS,
  )
  const parseCycles = (s: string) => clampInteger(
    parseInt(s, 10) || 0,
    0,
    GOAL_DRAFT_MAX_CYCLES,
  )

  // Only a genuine user edit should persist a draft. Seeding from the live loop
  // or reconciling another device's draft must NOT create a fresh edit time.
  const hasEdited = useRef(false)
  // Async clear completion must distinguish its own reset from text typed across
  // either request boundary even when React batches the parent loop update.
  const editRevision = useRef(0)
  const syncGeneration = useRef(0)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])
  // Latest field values, kept current every render so async reconciliation and
  // the close-flush can prove they still refer to the same open editor.
  const latest = useRef({ slotKey, message, idleInput, maxCyclesInput, loop, open })
  latest.current = { slotKey, message, idleInput, maxCyclesInput, loop, open }

  function applyDraft(draft: GoalDraft | null) {
    setMessage(clampGoalDraftMessage(draft ? draft.message : DEFAULT_MSG))
    setIdleInput(String(draft ? draft.idleSecs : 60))
    setMaxCyclesInput(String(draft ? draft.maxCycles : 0))
  }

  // Compute the draft to persist for the current field state, or null to drop
  // the slot: the blank / pristine-default case stores nothing so an emptied or
  // untouched popover never pins the template.
  function draftToPersist(s: typeof latest.current): GoalDraft | null {
    const idleSecs = parseIdle(s.idleInput)
    const maxCycles = parseCycles(s.maxCyclesInput)
    const isPristineDefault = s.message === DEFAULT_MSG && idleSecs === 60 && maxCycles === 0
    return isPristineDefault ? null : { message: s.message, idleSecs, maxCycles }
  }

  const acceptedLiveDraft = useRef<{ slot: string; snapshot: GoalDraftSnapshot } | null>(null)

  function editorStillMatches(snapshot: GoalDraftSnapshot): boolean {
    return latest.current.open
      && !latest.current.loop
      && sameGoalDraft(draftToPersist(latest.current), snapshot.draft)
  }

  function applyCanonicalIfCurrent(
    targetSlot: string,
    submitted: GoalDraftSnapshot,
    canonical: GoalDraftSnapshot,
    result: Exclude<DraftPersistResult, 'remote-failed'>,
  ): void {
    if (
      result === 'superseded'
      || !mounted.current
      || latest.current.slotKey !== targetSlot
      || !editorStillMatches(submitted)
    ) return
    hasEdited.current = false
    acceptedLiveDraft.current = { slot: targetSlot, snapshot: canonical }
    applyDraft(canonical.draft)
  }

  const writeRemoteDraft = (
    targetSlot: string,
    snapshot: GoalDraftSnapshot,
    options: { keepalive?: boolean; migration?: boolean },
  ) => remoteDraftMutation.mutateAsync({ targetSlot, snapshot, options })

  function enqueueRemoteDraft(
    targetSlot: string,
    snapshot: GoalDraftSnapshot,
    options: { keepalive?: boolean; migration?: boolean } = {},
  ): Promise<GoalDraftSnapshot> {
    return enqueueRemoteGoalDraft(targetSlot, snapshot, writeRemoteDraft, options)
  }

  function persistDraft(
    targetSlot: string,
    snapshot: GoalDraftSnapshot,
    keepalive = false,
  ): Promise<DraftPersistResult> {
    return enqueueRemoteDraft(targetSlot, snapshot, { keepalive })
      .then(canonical => {
        const result = cacheCanonicalGoalDraft(targetSlot, snapshot, canonical)
        applyCanonicalIfCurrent(targetSlot, snapshot, canonical, result)
        return result
      })
      .catch(() => {
        // Offline/local-only remains the fallback, but an open editor must say
        // that the shared copy is stale and offer the same migration retry path.
        if (
          !keepalive
          && mounted.current
          && latest.current.open
          && latest.current.slotKey === targetSlot
          && !latest.current.loop
          && editorStillMatches(snapshot)
        ) {
          setDraftSyncState(syncFailureFor(snapshot))
        }
        return 'remote-failed'
      })
  }

  function persistLocalDraft(
    targetSlot: string,
    draft: GoalDraft | null,
    keepalive = false,
  ): Promise<DraftPersistResult> {
    return persistDraft(targetSlot, savePendingGoalDraft(targetSlot, draft), keepalive)
  }

  function persistCurrentDraft(s: typeof latest.current, keepalive = false) {
    return persistLocalDraft(s.slotKey, draftToPersist(s), keepalive)
  }

  /**
   * Invariant: every newer cross-tab snapshot accepted into the live editor is
   * already the server canonical or is queued exactly once behind this slot's
   * write tail. Failure stays visible for Retry; a later local edit prevents an
   * older answer from replacing the editor.
   */
  async function adoptNewerCrossTabDraft(
    candidate: GoalDraftSnapshot,
    knownCanonical?: GoalDraftSnapshot,
  ): Promise<boolean> {
    if (
      !mounted.current
      || !latest.current.open
      || latest.current.loop
      || hasEdited.current
      || latest.current.slotKey !== slotKey
    ) return false

    const accepted = acceptedLiveDraft.current?.slot === slotKey
      ? acceptedLiveDraft.current.snapshot
      : null
    if (accepted) {
      if (candidate.updatedAt < accepted.updatedAt) return false
      if (sameGoalDraftSnapshot(candidate, accepted)) return false
    }

    acceptedLiveDraft.current = { slot: slotKey, snapshot: candidate }
    applyDraft(candidate.draft)
    setDraftSyncState('updated')
    const outcome = await syncAcceptedCrossTabGoalDraft(
      slotKey,
      candidate,
      knownCanonical,
      writeRemoteDraft,
    )
    const stillShowingCandidate = (
      mounted.current
      && latest.current.open
      && !latest.current.loop
      && !hasEdited.current
      && latest.current.slotKey === slotKey
      && acceptedLiveDraft.current?.slot === slotKey
      && sameGoalDraftSnapshot(acceptedLiveDraft.current.snapshot, candidate)
      && editorStillMatches(candidate)
    )
    if (!stillShowingCandidate) return true
    if (outcome.result === 'remote-failed') {
      setDraftSyncState(syncFailureFor(candidate))
      return true
    }
    if (outcome.canonical && outcome.result !== 'superseded') {
      acceptedLiveDraft.current = { slot: slotKey, snapshot: outcome.canonical }
      applyDraft(outcome.canonical.draft)
    }
    setDraftSyncState(
      outcome.result === 'retained'
        ? syncFailureFor(outcome.canonical ?? candidate)
        : 'updated',
    )
    return true
  }

  // V2 appends emit one event for their immutable primary key. Legacy writers
  // commit the timestamp sidecar before the body; ignore that unpaired event
  // and reconcile only when the body-key event proves both writes are visible.
  useEffect(() => {
    if (!open || loop) return
    const onStorage = (event: StorageEvent) => {
      if (event.storageArea && event.storageArea !== localStorage) return
      if (!isGoalDraftStorageKey(event.key)) return
      void adoptNewerCrossTabDraft(latestLocalGoalDraft(slotKey))
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- listener is scoped by open/slot/loop; refs hold the live editor and queue state
  }, [open, slotKey, loop])

  /* A pending confirmation belongs to the record the reader was LOOKING at. The
     popover re-renders from websocket state without closing, so another tab can
     swap that record underneath it -- edit and restart the same loop id, then a
     cycle cap (max_cycles=1 fires once) stops it again -- and the primed press
     would erase a goal the confirmation never described. The intent guard does
     not catch it: the record is inactive at render AND at press, so the server
     sees no mismatch. Keyed on identity, state and the text itself, since the
     text is what the erase destroys and drafts are not persisted while a loop
     exists. */

  useEffect(() => {
    setConfirmClear(false)
  }, [loop?.id, loop?.active, loop?.message])

  // Seed/restore fields on each open (rising edge). A live loop is the
  // authoritative source. Otherwise show the local fallback immediately, then
  // reconcile with the shared server copy. Existing local-only installs migrate
  // by timestamp: the newer browser copy is uploaded, while an older mobile copy
  // cannot overwrite a newer desktop value. A response never replaces text the
  // user started editing while the request was in flight.
  useEffect(() => {
    if (!open) {
      syncGeneration.current += 1
      setDraftSyncState('idle')
      return
    }
    const generation = ++syncGeneration.current
    hasEdited.current = false
    setError('')
    setConfirmClear(false)
    if (loop) {
      setDraftSyncState('idle')
      setMessage(clampGoalDraftMessage(loop.message || DEFAULT_MSG))
      setIdleInput(String(loop.idle_secs || 60))
      setMaxCyclesInput(String(loop.max_cycles || 0))
      return
    }

    const local = latestLocalGoalDraft(slotKey)
    acceptedLiveDraft.current = { slot: slotKey, snapshot: local }
    applyDraft(local.draft)
    setDraftSyncState('checking')
    const stillCurrent = () => (
      syncGeneration.current === generation
      && latest.current.open
      && latest.current.slotKey === slotKey
      && !latest.current.loop
      && !hasEdited.current
    )
    const adoptAdvancedLocal = async (
      knownCanonical?: GoalDraftSnapshot,
    ): Promise<boolean> => {
      const accepted = acceptedLiveDraft.current
      // A storage event may already have adopted and queued the newer snapshot.
      // In that case this reconciliation must retire its stale local baseline.
      if (
        accepted?.slot === slotKey
        && !sameGoalDraftSnapshot(accepted.snapshot, local)
      ) return true
      return adoptNewerCrossTabDraft(latestLocalGoalDraft(slotKey), knownCanonical)
    }
    void (async () => {
      try {
        const result = await refetchRemoteDraft({ throwOnError: true })
        const remote = result.data
        if (!remote || !stillCurrent()) return
        // GET may have waited while another tab advanced shared localStorage.
        // Accepting that browser copy also queues it once unless this GET proves
        // the exact snapshot is already canonical.
        if (await adoptAdvancedLocal(remote)) return
        let canonical = remote
        const localConflictsAtSameStamp = local.updatedAt === remote.updatedAt
          && !sameGoalDraft(local.draft, remote.draft)
        if (local.updatedAt > remote.updatedAt || localConflictsAtSameStamp) {
          const migration = await syncAcceptedCrossTabGoalDraft(
            slotKey,
            local,
            remote,
            writeRemoteDraft,
            { retryFailed: true },
          )
          if (migration.result === 'remote-failed' || !migration.canonical) {
            if (stillCurrent()) setDraftSyncState(syncFailureFor(local))
            return
          }
          canonical = migration.canonical
        }
        if (!stillCurrent()) return
        // The migration request is another await boundary; re-check again.
        if (await adoptAdvancedLocal(canonical)) return
        // A full store can accept and immediately evict an older migration.
        // Keep the browser's only copy instead of turning that response into a
        // local tombstone and resetting the open editor.
        if (local.updatedAt > 0 && canonical.updatedAt === 0) {
          setDraftSyncState(syncFailureFor(local))
          return
        }
        const changed = !sameGoalDraft(local.draft, canonical.draft)
        let cacheResult: Exclude<DraftPersistResult, 'remote-failed'> = 'persisted'
        if (canonical.updatedAt > 0) {
          cacheResult = cacheCanonicalGoalDraft(slotKey, local, canonical)
          if (cacheResult === 'superseded') {
            await adoptNewerCrossTabDraft(latestLocalGoalDraft(slotKey), canonical)
            return
          }
        }
        acceptedLiveDraft.current = { slot: slotKey, snapshot: canonical }
        applyDraft(canonical.draft)
        setDraftSyncState(
          cacheResult === 'retained'
            ? syncFailureFor(canonical)
            : (changed ? 'updated' : 'idle'),
        )
      } catch {
        // The read itself failed. The retained local snapshot is what Retry
        // will reconcile, so it also decides whether this is a draft or a clear.
        if (stillCurrent()) setDraftSyncState(syncFailureFor(local))
      }
    })()
    return () => {
      if (syncGeneration.current === generation) syncGeneration.current += 1
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- open-edge/retry seed only; refs guard the async continuation against slot/loop/edit changes
  }, [open, draftSyncAttempt])

  // Flush a pending debounced edit synchronously into local storage when the
  // popover closes or unmounts, then use a keepalive request for the shared copy.
  useEffect(() => {
    if (!open) return
    return () => {
      if (!hasEdited.current || latest.current.loop) return
      void persistCurrentDraft(latest.current, true)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- stable cleanup reading the latest ref
  }, [open])

  // Persist genuine edits per slot, debounced with the same interval as chat
  // drafts. Local storage remains the immediate/offline commit; the server write
  // makes the same latest draft visible to every dashboard client.
  useEffect(() => {
    if (!open || !hasEdited.current || loop) return
    const timer = setTimeout(
      () => { void persistCurrentDraft(latest.current) },
      DRAFT_SAVE_DEBOUNCE_MS,
    )
    return () => clearTimeout(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- pure ref snapshot; function identity must not restart the debounce
  }, [open, slotKey, message, idleInput, maxCyclesInput, loop])

  async function save() {
    if (writeDisabled || draftSyncState === 'checking') return
    setSaving(true)
    setError('')
    try {
      // Parse from the raw strings here (not a committed number state) so a value
      // typed and then Save-clicked without an intervening blur is still captured.
      const idle_secs = parseIdle(idleInput)
      const max_cycles = parseCycles(maxCyclesInput)
      const body = JSON.stringify({ slot_key: slotKey, message, idle_secs, max_cycles })
      const resp = loop
        ? await fetch(`/api/autonudge/${loop.id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message, idle_secs, max_cycles, active: true }) })
        : await fetch('/api/autonudge', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body })
      const data = await resp.json()
      if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`)
      if (!loop && hasEdited.current) {
        void persistLocalDraft(slotKey, draftToPersist(latest.current))
      }
      onChange(data.loop)
      onOpenChange(false)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  async function stop() {
    if (!loop) return
    setSaving(true)
    try {
      // The INTENT travels with the request, because the server otherwise
      // decides what this verb means from the record's state at arrival time:
      // a press meant as "Stop loop" on a popover rendered moments earlier
      // would silently ERASE a record that went terminal in between. The server
      // 409s on a mismatch instead, and the popover surfaces that.
      const intent = loop.active ? 'stop' : 'clear'
      // Clearing removes the loop before it writes the draft tombstone. Capture
      // the editor the user submitted so an edit typed across either await can
      // become the next draft instead of being mistaken for cleared state.
      const clearSlot = slotKey
      const clearSubmission = intent === 'clear' ? draftToPersist(latest.current) : null
      const clearEditRevision = editRevision.current
      const resp = await fetch(`/api/autonudge/${loop.id}?intent=${intent}`, { method: 'DELETE' })
      if (!resp.ok) {
        // Parse JSON body for server-supplied error (e.g. 503 when feature disabled).
        // Only on error path: a successful DELETE may return 204 No Content.
        const data = await resp.json().catch(() => ({}))
        throw new Error(data.error || `HTTP ${resp.status}`)
      }
      if (intent === 'clear') {
        const editorMatchesSubmission = editRevision.current === clearEditRevision
          && latest.current.slotKey === clearSlot
          && sameGoalDraft(draftToPersist(latest.current), clearSubmission)
        onChange(null)
        // The loop record is already gone, but an edit typed while DELETE was
        // pending belongs to the now-inactive slot. Do not reset it. Once the
        // parent drops `loop`, the ordinary debounce makes it durable and queues
        // it behind the clear exactly once.
        if (editorMatchesSubmission) {
          hasEdited.current = false
          applyDraft(null)
          setDraftSyncState('checking')
        } else {
          setDraftSyncState('idle')
        }
        const clearSnapshot = savePendingGoalDraft(clearSlot, null)
        const result = await persistDraft(clearSlot, clearSnapshot)
        if (!mounted.current) return
        // A second edit can land while the tombstone PUT is pending. Only the
        // unchanged cleared editor belongs to that completion; a newer editor
        // stays open and its own queued write/retry owns the visible sync state.
        const clearStillOwnsEditor = editRevision.current === clearEditRevision
          && latest.current.slotKey === clearSlot
          && latest.current.open
        if (!clearStillOwnsEditor) {
          setDraftSyncState('idle')
          return
        }
        if (result === 'persisted') {
          onOpenChange(false)
        } else if (result === 'superseded') {
          // A newer durable edit won without changing this editor. Keep it open
          // so storage-event reconciliation or Retry can adopt that value.
          setDraftSyncState('idle')
        } else {
          // Both failure modes retain the tombstone locally or in the module
          // fallback. Retry/reopen reconciles that same newest snapshot. The
          // editor already shows the cleared template, so the notice names the
          // clear that did not finish rather than a draft that is not there.
          setDraftSyncState('clear-failed')
        }
        return
      }
      onChange(null)
      onOpenChange(false)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  /** Run the loop's next cycle now instead of waiting out the remaining gap.
   *
   *  Sends NO body: the nudge fired is whatever the loop currently holds, read
   *  server-side, so the button stays correct after a `monitor_update` revises
   *  the instruction and a stale popover field can never be delivered as the
   *  prompt. The consequence is that a user who edited the message and pressed
   *  this gets the ARMED message, not the edited one.
   *
   *  WHICH IS WHY THIS DOES NOT CLOSE THE POPOVER, unlike `save` and `stop`.
   *  Closing would drop that unsaved edit with no dirty guard (drafts are not
   *  persisted while a loop exists), so a press after an edit would cost the
   *  user their text as well as spending a turn on the old prompt. Leaving the
   *  popover open keeps the edit, keeps Save reachable, and makes the outcome
   *  visible in place: the schedule line beside the button flips to "due", and
   *  the header's cycle readout advances a moment later when the delivered fire
   *  broadcasts (`autonudge_state`), which is also where the press's cost
   *  against the cycle cap becomes observable.
   *
   *  Refusals (409 for a mid-fire loop or a session with a turn in flight, 404
   *  for a loop the server no longer holds) land in the same inline
   *  `ErrorNotice` as `save` and `stop`. */
  async function triggerNow() {
    if (!loop) return
    setSaving(true)
    setError('')
    try {
      const resp = await fetch(`/api/autonudge/${loop.id}/fire`, { method: 'POST' })
      const data = await resp.json().catch(() => ({}))
      if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`)
      // The route returns the loop UNCHANGED: the server-side deadline write was
      // removed because it could not be made durable without a suspension point
      // that raced several lock-free writers. Rendering the response verbatim
      // would therefore leave the countdown showing the very cycle this press
      // superseded -- the one visible confirmation a press has. So the armed
      // deadline is set here instead. Not a fiction: the cycle IS armed to run
      // now, and the delivery's `autonudge_state` frame reconciles the shared
      // cache moments later.
      onChange({ ...data.loop, next_due_ts: Date.now() / 1000 })
      // Keep the SHARED registry consistent with the local view. `onChange` only
      // updates this popover, so a reader of the full registry -- the Crew Members
      // patrol block -- would otherwise keep its cached copy until the delivery's
      // `autonudge_state` frame arrives. Nothing about the deadline changes here
      // any more, so this is about the two views never disagreeing rather than
      // about a stale countdown.
      void queryClient.invalidateQueries({ queryKey: AUTONUDGE_LOOPS_QUERY_KEY })
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  // ── Countdown to the next trigger (#6482) ──
  // The 1s ticker runs only while the popover is OPEN (review finding: a
  // closed-but-armed loop must not re-render the toolbar button every second
  // all day). The hover affordance needs no ticker: a native title tooltip
  // snapshots at hover-start, so the trigger's onMouseEnter/onFocus refresh
  // nowTs once, which is exactly the freshness a tooltip glance can show.
  const ticking = open && !!loop?.active && (loop.next_due_ts || 0) > 0
  const [nowTs, setNowTs] = useState(() => Date.now() / 1000)
  useEffect(() => {
    if (!ticking) return
    setNowTs(Date.now() / 1000)
    const timer = setInterval(() => setNowTs(Date.now() / 1000), 1000)
    return () => clearInterval(timer)
  }, [ticking])
  const refreshNow = () => setNowTs(Date.now() / 1000)
  /** Hover/popover line for the next trigger, or '' when no active loop — the
   *  shared deadline-preserving reading (see `nextCycleText`). */
  const countdownText = nextCycleText(loop, nowTs)
  /** The tooltip only carries a REAL deadline signal (counting or due) — the
   *  "not yet scheduled" placeholder is popover-only, so an armed-but-unscheduled
   *  loop keeps the plain "Goal active (cycle N)" title. */
  const titleCountdown = loop?.active && (loop.next_due_ts || 0) > 0 ? countdownText : ''
  /** Cycle readout for the chip, tooltip and popover header ("3/24", or a
   *  bare "3" under an infinite cap). Interpolated as the {{cycle}} VALUE of
   *  the existing strings, so no catalogue text changes. Unlike the countdown
   *  this is safe in aria-label: it changes once per cycle, not once per
   *  second. */
  /** Whether a cycle is ALREADY armed to run. Derived from the same countdown
   *  the schedule line renders, so the button and the text can never disagree. */
  const cycleAlreadyDue =
    countdownText === i18nT('components.autoNudgePopover.next_cycle_due')
  /** Help line under the goal textarea while it carries the raw kill-switch
   *  token; '' otherwise. See the JSX comment at the render site (#10458). */
  const stopFileHelp = message.includes(STOP_FILE_TOKEN)
    ? loop && loop.stop_sentinel_path === ''
      ? i18nT('components.autoNudgePopover.stop_file_help_none', { token: STOP_FILE_TOKEN })
      : i18nT('components.autoNudgePopover.stop_file_help', { token: STOP_FILE_TOKEN })
    : ''
  const stopFileHelpId = useId()

  const cycleText = loopCycleText(loop)

  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      {trigger ? <PopoverTrigger asChild>{trigger}</PopoverTrigger> : (
      <PopoverTrigger asChild>
        <button
          className={`h-8 px-2 rounded-lg text-[12px] font-mono flex items-center gap-1 cursor-pointer transition-all bg-transparent border-none shrink-0 whitespace-nowrap ${
            loop?.active
              ? interrupted
                ? 'text-warn hover:text-warn hover:bg-warn/10'
                : 'text-accent hover:text-accent hover:bg-accent/10 animate-pulse'
              : 'text-muted hover:text-text hover:bg-bg-hover'
          }`}
          title={loop?.active ? `${interrupted ? i18nT('components.autoNudgePopover.goal_interrupted_cycle', { cycle: cycleText }) : i18nT('components.autoNudgePopover.goal_active_cycle', { cycle: cycleText })}${titleCountdown ? ` · ${titleCountdown}` : ''}` : i18nT('components.autoNudgePopover.set_a_goal')}
          // The countdown stays OUT of aria-label (review finding): a
          // per-second label change re-announces the button to screen readers.
          aria-label={loop?.active ? (interrupted ? i18nT('components.autoNudgePopover.goal_interrupted_cycle', { cycle: cycleText }) : i18nT('components.autoNudgePopover.goal_active_cycle', { cycle: cycleText })) : i18nT('components.autoNudgePopover.set_a_goal')}
          onMouseEnter={refreshNow}
          onFocus={refreshNow}
        >
          <Goal size={16} className="shrink-0" />
          {loop?.active && loop.cycle_count > 0 ? cycleText : null}
        </button>
      </PopoverTrigger>
      )}
      {content ?? <PopoverContent
        side="top"
        align="start"
        /* Viewport-capped rather than a pinned 420px: at the 320px floor a fixed
           width pushes this panel -- and the right-aligned action below -- past the
           usable viewport. Written as a max so there is no `md:` counterpart to keep
           in sync: 420px is simply the ceiling, and a phone gets the width it has. */
        className="w-[min(calc(100vw-1rem),26.25rem)] max-h-[min(80vh,42rem)] overflow-y-auto p-4 text-[12px]"
      >
        <div className="flex items-center justify-between mb-2">
          <div className="flex items-center gap-2 font-medium text-text">
            <Goal size={14} className={loop?.active ? 'text-accent' : 'text-muted'} />
            {i18nT('components.autoNudgePopover.set_a_goal')}
            {loop?.active && <span className="text-muted text-[11px]">{i18nT('components.autoNudgePopover.cycle')} {cycleText}</span>}
          </div>
          <button aria-label={i18nT('components.autoNudgePopover.close')} onClick={() => onOpenChange(false)} className="text-muted hover:text-text bg-transparent border-none cursor-pointer">
            <X size={14} />
          </button>
        </div>
        {onSetUpBoundedMonitor ? (
          <>
            {/* An OFFER, not a way back: this editor is the view the popover
                opens on, so a reader arriving here has no bounded monitor
                behind them to return to. Hence a Radar glyph rather than a left
                arrow, and a label naming the SUBJECT that surface takes -- it
                accepts a pull request URL and nothing else, so a label reading
                only "bounded monitor" walks a reader with any other goal into
                a form whose one field they cannot fill.
                Underlined without hovering, because this is now the ONLY route
                to the monitor: a usability reader could not tell 11px muted
                text was clickable at all, and a hover-only affordance is
                invisible on a touch viewport. */}
            <button
              type="button"
              onClick={onSetUpBoundedMonitor}
              className="mb-2 inline-flex items-center gap-1 border-none bg-transparent p-0 text-[11px] text-muted underline cursor-pointer hover:text-text"
            >
              <Radar size={13} className="lucide-inline" aria-hidden />
              {i18nT('components.sessionAutomationPopover.set_up_bounded_monitor')}
            </button>
            {/* Warn-coloured, unchanged from when this form was opt-in. Muting
                it read better to the author and worse to review: on the view
                every reader now lands on, this sentence is the only cost cue
                the surface carries, and dropping its colour weakened that cue
                in the same change that made the surface the default. */}
            <p role="note" className="mb-2 rounded-md border border-warn/30 bg-warn-subtle px-2 py-1.5 text-[11px] text-warn-fg">
              {i18nT('components.sessionAutomationPopover.legacy_notice')}
            </p>
          </>
        ) : null}
        <p className="text-muted text-[11px] mb-3 leading-relaxed">{i18nT('components.autoNudgePopover.give_the_agent_a_goal_and_it_will_keep_working_t')}</p>

        {!loop && (draftSyncState === 'failed' || draftSyncState === 'clear-failed') ? (
          <div className="flex items-center justify-between gap-2 mb-3">
            {/* No hand-off: this popover retains the unsaved goal draft (or the
                clear tombstone) and its retry must stay beside that editable copy. */}
            <ErrorNotice
              variant="inline"
              testId="goal-draft-sync-error"
              message={draftSyncState === 'clear-failed'
                ? i18nT('components.autoNudgePopover.draft_clear_sync_failed')
                : i18nT('components.autoNudgePopover.draft_sync_failed')}
            />
            <button
              type="button"
              onClick={() => {
                setDraftSyncState('checking')
                setDraftSyncAttempt(attempt => attempt + 1)
              }}
              className="px-2 py-0.5 rounded border border-border text-[11px] text-muted hover:text-text bg-transparent cursor-pointer shrink-0"
            >
              {i18nT('components.autoNudgePopover.retry')}
            </button>
          </div>
        ) : !loop && draftSyncState !== 'idle' ? (
          <p
            role="status"
            data-testid="goal-draft-sync-status"
            className="mb-3 text-[11px] leading-relaxed text-muted"
          >
            {draftSyncState === 'checking'
              ? i18nT('components.autoNudgePopover.draft_sync_checking')
              : i18nT('components.autoNudgePopover.draft_sync_updated')}
          </p>
        ) : null}

        {watchesFailed && (
          <div className="flex items-center justify-between gap-2 mb-3">
            {/* No hand-off: the popover holds the unsaved goal message, idle and max-cycle inputs.
                Retry is the recovery path, as on every sibling load-failure notice. */}
            <ErrorNotice
              variant="inline"
              testId="auto-nudge-watches-error"
              message={i18nT('components.autoNudgePopover.watches_load_failed')}
            />
            <button
              type="button"
              onClick={() => { void refetchWatches() }}
              className="px-2 py-0.5 rounded border border-border text-[11px] text-muted hover:text-text bg-transparent cursor-pointer shrink-0"
            >
              {i18nT('components.autoNudgePopover.retry')}
            </button>
          </div>
        )}

        {watches.length > 0 && (
          <div className="border border-border rounded p-2 mb-3">
            <div className="text-text text-[11px] font-medium mb-1">
              {i18nT('components.autoNudgePopover.watches_title')}
            </div>
            <ul className="list-none p-0 m-0 mb-1">
              {watches.map(w => (
                <li key={w.id} className="text-muted text-[11px] leading-relaxed">
                  <span className="text-text">{w.name}</span>
                  {w.schedule && <span> · {w.schedule}</span>}
                  {w.next_run_ts && (
                    <span> · {i18nT('components.autoNudgePopover.watches_next')} {fmtTimeNumeric(w.next_run_ts)}</span>
                  )}
                </li>
              ))}
            </ul>
            <div className="text-muted text-[11px] leading-relaxed">
              {i18nT('components.autoNudgePopover.watches_note')}
            </div>
          </div>
        )}

        {/* The reason the fields below are dead. `writeDisabled` alone renders a
            form a crew/member reader cannot use and does not say why: the
            explanation used to live on the bounded view, which was the default,
            and making the goal loop the default left the disabled form with no
            reason attached.
            Rendered from the boolean rather than through a `reason` prop. The
            prop was a one-consumer generalization -- its single caller passed
            one constant gated on this same condition -- and the rationale for
            it ("the editor knows nothing about session modes") was already
            false, since this component reads `sessionAutomationPopover` strings
            two lines up. A second reason for disabling writes would need the
            reason back as a parameter; there is exactly one today. */}
        {writeDisabled ? (
          <p
            role="status"
            data-testid="auto-nudge-write-disabled-reason"
            className="mb-3 rounded-md border border-border bg-bg px-2 py-1.5 text-[11px] leading-relaxed text-muted"
          >
            {i18nT('components.sessionAutomationPopover.session_mode_unavailable')}
          </p>
        ) : null}

        <div className="text-muted text-[11px] mb-1">{i18nT('components.autoNudgePopover.goal_description')}</div>
        <textarea
          aria-label={i18nT('components.autoNudgePopover.goal_description')}
          aria-describedby={[messageLimitId, stopFileHelp ? stopFileHelpId : ''].filter(Boolean).join(' ')}
          value={message}
          disabled={writeDisabled}
          onChange={e => {
            editRevision.current += 1
            hasEdited.current = true
            setDraftSyncState('idle')
            setMessage(clampGoalDraftMessage(e.target.value))
          }}
          rows={6}
          className="w-full bg-bg border border-border rounded p-2 text-[12px] font-mono resize-y mb-1 text-text"
          placeholder={i18nT('components.autoNudgePopover.describe_what_you_want_the_agent_to_accomplish')}
        />
        <p
          id={messageLimitId}
          data-testid="goal-message-character-count"
          className={`mb-3 text-right text-[11px] ${messageLength === GOAL_DRAFT_MAX_MESSAGE_CHARS ? 'text-warn' : 'text-muted'}`}
        >
          {i18nT('components.autoNudgePopover.goal_message_character_count', {
            count: messageLength,
            limit: GOAL_DRAFT_MAX_MESSAGE_CHARS,
          })}
        </p>
        {/* One element for everyone. It is always mounted so the polite live
            region exists before its text changes (a region created together
            with its content is not reliably announced), and it is visible while
            the limit is hit: a pasted goal that lost its tail was otherwise
            signalled to sighted users only by the small counter above turning
            colour. Empty, it takes no space and stays out of the tab order. */}
        <p
          role="status"
          aria-live="polite"
          data-testid="goal-message-limit-warning"
          className={messageLength === GOAL_DRAFT_MAX_MESSAGE_CHARS
            ? '-mt-2 mb-3 text-[11px] leading-relaxed text-warn'
            : 'sr-only'}
        >
          {messageLength === GOAL_DRAFT_MAX_MESSAGE_CHARS
            ? i18nT('components.autoNudgePopover.goal_message_limit_reached', {
                limit: GOAL_DRAFT_MAX_MESSAGE_CHARS,
              })
            : ''}
        </p>
        {stopFileHelp ? (
          /* Display-only explanation of the raw token above (#10458). The
             textarea keeps `{{STOP_FILE}}` because the server substitutes it
             when each nudge is sent; only the human reading the form needed
             telling what it turns into. Shown while the goal text carries the
             token, so a custom goal without it gets no orphan help line. The
             empty-sentinel arm reads the ARMED loop's record: a loop that
             carries an explicitly empty `stop_sentinel_path` has nothing to
             substitute, so the honest line is that the token goes out blank
             and Stop loop is the way to halt it. The path itself is never
             rendered: the websocket frame withholds it and this surface has no
             owner gate. */
          <p id={stopFileHelpId} className="text-muted text-[11px] leading-relaxed -mt-2 mb-3">
            {stopFileHelp}
          </p>
        ) : null}


        <div className="flex flex-col gap-3 mb-3 sm:flex-row">
          <div className="flex-1">
            <div className="text-muted text-[11px] mb-1">{i18nT('components.autoNudgePopover.seconds_between_nudges')}</div>
            <input
              type="number"
              aria-label={i18nT('components.autoNudgePopover.seconds_between_nudges')}
              min={GOAL_DRAFT_MIN_IDLE_SECS}
              max={GOAL_DRAFT_MAX_IDLE_SECS}
              value={idleInput}
              disabled={writeDisabled}
              onChange={e => { editRevision.current += 1; hasEdited.current = true; setDraftSyncState('idle'); setIdleInput(e.target.value) }}
              onBlur={() => setIdleInput(String(parseIdle(idleInput)))}
              className="w-full bg-bg border border-border rounded px-2 py-1 text-[12px] text-text"
            />
          </div>
          <div className="flex-1">
            <div className="text-muted text-[11px] mb-1">{i18nT('components.autoNudgePopover.max_cycles_0')}</div>
            <input
              type="number"
              aria-label={i18nT('components.autoNudgePopover.max_cycles_0_infinite')}
              min={0}
              max={GOAL_DRAFT_MAX_CYCLES}
              value={maxCyclesInput}
              disabled={writeDisabled}
              onChange={e => { editRevision.current += 1; hasEdited.current = true; setDraftSyncState('idle'); setMaxCyclesInput(e.target.value) }}
              onBlur={() => setMaxCyclesInput(String(parseCycles(maxCyclesInput)))}
              className="w-full bg-bg border border-border rounded px-2 py-1 text-[12px] text-text"
            />
          </div>
        </div>

        {/* The trigger sits on the SCHEDULE line, not in the action row below.
            Two reasons, and they point the same way. `max-two-buttons-per-row`
            (website/AUTOSDE.yaml:230, blocking) holds a row to two controls and
            names this exact escape -- "the third action ... goes into an
            overflow DropdownMenu, or LEAVES THE ROW" -- and leaving is cheaper
            than a menu for one action. And it belongs here on the merits: this
            button changes the countdown printed beside it, so the control and
            the state it acts on read as one thing, while Stop/Save act on the
            loop's configuration.
            A one-button group, so the cap is satisfied structurally rather than
            by being under it today. Button classes are the popover's existing
            small-button spelling (the watches Retry above).
            Gated on `active`, not merely on `loop`: a paused record still opens
            this popover, and every terminal bound leaves the loop inactive, so
            the server refuses to fire one -- a button there could only ever
            produce a 409. */}
        {loop && (
          /* `flex-wrap` is for STRING LENGTH, not for 320px: the width cap on the
             shell is what keeps this row inside the viewport, and measurement says
             so -- pinning the shell back to 420px reddens the narrow frame while
             removing this wrap does not. It is kept because `shrink-0` protects the
             button, so a longer localized countdown ("Next cycle due, fires after
             the current turn" is materially longer in several of the twelve
             catalogues) has only this row to give. Defensive, and labelled as such
             rather than claimed as the fix. */
          /* STACKED in every state, not a wrapping row. When the countdown flips to
             the longer "due" wording, a wrapping row moved the button from beside the
             text onto its own line -- relocating a control directly under the cursor
             that just pressed it. One layout at every width also means the narrow
             frame and the desktop frame agree, instead of the 320px case being a
             second shape to keep in sync. */
          <div className="flex flex-col items-start gap-1 mb-3">
            <div className="text-muted text-[11px]">
              {i18nT('components.autoNudgePopover.last_fire')} {loop.last_fire_ts ? fmtTimeNumeric(loop.last_fire_ts) : i18nT('components.autoNudgePopover.never')}
              {countdownText && <span> · {countdownText}</span>}
            </div>
            {loop.active ? (
              <button
                type="button"
                onClick={triggerNow}
                /* Disabled once a cycle is already due, which is what a successful
                   press produces. Before this the button re-enabled unchanged, so
                   the press acknowledged itself only through the schedule line's
                   wording -- a usability reader would not press it a second time
                   because they could not tell whether that would double the nudge
                   or do nothing (it does nothing: the cycle is already armed). The
                   disabled state answers that question without a new string. */
                disabled={saving || cycleAlreadyDue}
                className="px-2 py-0.5 rounded border border-border text-[11px] text-muted hover:text-text hover:border-accent bg-transparent cursor-pointer shrink-0 disabled:opacity-50"
              >
                {i18nT('components.autoNudgePopover.trigger_nudge')}
              </button>
            ) : (
              /* Says WHY the button is not here, rather than leaving a gap. A
                 blind reader of the stopped screenshot could not tell it was the
                 same loop at all, and an inactive loop otherwise looks identical
                 to an active one whose button failed to render -- the state is
                 the reason for the absence, so it belongs in the space the
                 absence leaves. Text, not a disabled button: the server refuses
                 to fire an inactive loop, so there is no press to offer.
                 Reads "Stopped", not "Paused": the button beside it removes this
                 record for good, and a blind reader took "Paused" as "it
                 remembers where it left off" -- a resumable-sounding status next
                 to an erase control is the mixed message a UX review blocked on.
                 The help line under it names both exits, because the erase is
                 irreversible and nothing else on the surface says so. */
              <div className="flex flex-col items-start gap-0.5">
                <span
                  data-testid="auto-nudge-loop-paused"
                  className="text-muted text-[11px] shrink-0"
                >
                  {i18nT('components.autoNudgePopover.loop_stopped')}
                </span>
                {/* While confirming, this line must not keep naming the two
                    buttons that just left the row -- a blind reader looked for
                    the "Start loop" it describes and could not find it -- and
                    the confirmation row itself renders no question. So the help
                    line BECOMES the question for that state. */}
                <span data-testid="auto-nudge-stopped-help" className="text-muted text-[11px]">
                  {confirmClear
                    ? i18nT('components.autoNudgePopover.clear_goal_question')
                    : i18nT('components.autoNudgePopover.stopped_help')}
                </span>
              </div>
            )}
          </div>
        )}

        {/* No hand-off: the popover holds the unsaved goal message, idle and max-cycle inputs. */}
        <ErrorNotice
          variant="inline"
          className="mb-2"
          testId="auto-nudge-error"
          message={error}
          onDismiss={() => setError('')}
        />

        <div className="flex gap-2 justify-end">
          {loop && (
            loop.active ? (
              <button
                onClick={stop}
                disabled={saving}
                className="px-3 py-1 rounded border border-border text-muted hover:text-danger hover:border-danger bg-transparent cursor-pointer disabled:opacity-50"
              >
                {i18nT('components.autoNudgePopover.stop_loop')}
              </button>
            ) : confirmClear ? (
              /* The same two-step the monitor surface uses for its identical
                 erase. Each label restates the ACTION and its object rather
                 than answering a question the row does not render: read alone,
                 "Yes" says nothing about what is being cleared. */
              <>
                <Btn type="button" onClick={() => setConfirmClear(false)} disabled={saving}>
                  {i18nT('components.autoNudgePopover.cancel')}
                </Btn>
                <Btn type="button" danger onClick={stop} disabled={saving}>
                  {i18nT('components.autoNudgePopover.clear_goal_for_good')}
                </Btn>
              </>
            ) : (
              /* On an already-stopped loop this press REMOVES the record, which
                 is what frees the slot to watch something else -- labelling it
                 "Stop loop" made it read as a no-op. It names the GOAL rather
                 than an internal noun, because a blind reader refused to press
                 "Clear record" for showing nothing called a record.
                 `Btn danger` colours it unconditionally rather than on :hover,
                 which a touch viewport never produces, and it sits behind a
                 confirm because it is an irreversible erase one slot from the
                 primary CTA -- the monitor surface's identical erase is guarded
                 exactly so. */
              <Btn type="button" danger onClick={() => setConfirmClear(true)} disabled={saving}>
                {i18nT('components.autoNudgePopover.clear_stopped_goal')}
              </Btn>
            )
          )}
          {/* Withheld while the clear is being confirmed: three controls in one
              row breaks the two-per-row cap (website/AUTOSDE.yaml:230), and the
              confirmation should hold the reader's whole choice -- the monitor
              surface's own confirm replaces its row for the same reason. */}
          {!confirmClear && (
            <button
              onClick={save}
              disabled={saving || writeDisabled || draftSyncState === 'checking' || !message.trim()}
              className="px-3 py-1 rounded bg-accent text-accent-fg border-none cursor-pointer disabled:opacity-50 hover:bg-accent/90"
            >
              {/* A paused loop's way out was invisible: this button silently PATCHes
                  `active: true`, so on an inactive loop it must SAY so. A usability
                  reader found no resume control at all and called both "Stopped" and
                  "Stop loop" risky as a result. Gated on `active`, not on existence,
                  which is the bug -- and it reuses the `start_loop` key the no-loop
                  case already uses, so no catalogue gains a string. */}
              {loop?.active
                ? i18nT('components.autoNudgePopover.save')
                : i18nT('components.autoNudgePopover.start_loop')}
            </button>
          )}
        </div>
      </PopoverContent>}
    </Popover>
  )
}
