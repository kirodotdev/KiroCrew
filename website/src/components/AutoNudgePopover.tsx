import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Goal, X } from 'lucide-react'
import { Popover, PopoverTrigger, PopoverContent } from './ui/popover'
import { Btn } from './ui'
import ErrorNotice from './ErrorNotice'
import { cronJobsQuery } from '../api/cronJobsQuery'
import { runBelongsToSlot } from '../apps/workflows/runModel'
import { loadGoalDraft, saveGoalDraft, type GoalDraft } from '../utils/goalDrafts'
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
  /** Present only when the legacy form is nested under the bounded monitor picker. */
  onBackToBoundedMonitor?: () => void
  /** Disable legacy-loop writes while leaving Stop available for stale state. */
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

const DEFAULT_MSG = `Your north star is in north_star.md, roadmap in roadmap.md, tasks in tasks.md. Pick the single highest-leverage next step toward the goal and execute it. Update tasks.md. Post a blocker ONCE if genuinely stuck. To halt the loop, create {{STOP_FILE}}`

/** One armed script cron owned by this chat slot. */
interface SlotWatch {
  id: string
  name: string
  schedule: string
  next_run_ts: number | null
}

export default function AutoNudgePopover({ slotKey, loop, open, onOpenChange, onChange, onBackToBoundedMonitor, writeDisabled = false, interrupted = false, trigger, content }: Props) {
  // `||` (not `??`) is deliberate on the loop tier: it preserves the fallback
  // so a loop with idle_secs/max_cycles of 0 or an empty message still shows
  // the 60 / 0 / default template rather than a bare 0 / "".
  const [message, setMessage] = useState(() => loop?.message || DEFAULT_MSG)
  // Idle-seconds and max-cycles are held as RAW STRINGS while the popover is
  // open so every edit (including a fully-cleared field or a transient "") is
  // allowed as-typed. Coercing to a number on each keystroke would snap a
  // backspaced-to-empty field straight back to its default and prevent removing
  // the leading digit. The string is parsed
  // into a number only when the field commits (blur / save); an empty or
  // unparseable value falls back to the field default — 60 idle, 0 cycles.
  const [idleInput, setIdleInput] = useState(() => String(loop?.idle_secs || 60))
  const [maxCyclesInput, setMaxCyclesInput] = useState(() => String(loop?.max_cycles || 0))
  const [saving, setSaving] = useState(false)
  /* Two-step on the clear only. The erase is irreversible and sits beside the
     primary CTA, so one press asks and the second performs. */
  const [confirmClear, setConfirmClear] = useState(false)
  const [error, setError] = useState('')
  // `ignored` is the server declining the submitted text; `kept` is the user declining to
  // overwrite. One state, not two booleans, since the two can never be true at once.
  const [saveNotice, setSaveNotice] = useState<'ignored' | 'kept' | null>(null)
  // Armed when Save would overwrite a REDACTED goal with the mask the user was shown.
  const [confirmOverwrite, setConfirmOverwrite] = useState(false)
  const [escapeWarned, setEscapeWarned] = useState(false)
  // Render scope, not save-local: the confirm must disappear the moment the edit is
  // reverted, or a settings-only save carries a destructive "replace" label.
  // Whether the USER typed in the goal textarea this open. The goal patch is gated on
  // this, never on comparing against a `loop` a live update can replace underneath us.
  const goalEdited = useRef(false)
  // Whether the USER changed either number this open. Same reason as the goal ref above:
  // a websocket update replacing `loop` must not read as an edit nobody made.
  const numericEdited = useRef(false)
  const parseIdle = (s: string) => parseInt(s, 10) || 60
  const parseCycles = (s: string) => parseInt(s, 10) || 0
  // Latched at OPEN, because both inputs below are live: a websocket update replacing `loop`
  // with a newer clean goal must neither disarm the gate nor become its own baseline.
  const [redactedAtOpen, setRedactedAtOpen] = useState(false)
  const [servedAtOpen, setServedAtOpen] = useState<string | null>(null)
  // Latched at OPEN like ``servedAtOpen``: arming on the LIVE token let a goal replaced
  // before the first Save click become its own baseline, so the 409 could never fire.
  const [fingerprintAtOpen, setFingerprintAtOpen] = useState<string | null>(null)
  // The goal the CONFIRM was armed on, latched when the gate appears. A render-time check
  // cannot close the race: the live goal can move between the read and the click.
  const [confirmArmedFor, setConfirmArmedFor] = useState<string | null>(null)
  // Re-arming is otherwise signalled ONLY by the preview text changing, which a screen
  // reader is never told, so the gate silently swallows the click that looked like a yes.
  const [confirmRearmed, setConfirmRearmed] = useState(false)
  // The BASELINE the confirm was armed on. Latched apart from the text above, which is a
  // redacted projection two different goals can share and so cannot identify one.
  const [confirmArmedFingerprint, setConfirmArmedFingerprint] = useState<string | null>(null)
  // Both arms require an actual edit: `save` gates the patch on `goalEdited.current`, so
  // arming without one promises a destruction that cannot happen.
  const editsRedactedGoal =
    goalEdited.current &&
    (Boolean(loop?.message_redacted) || redactedAtOpen) &&
    message !== (loop?.message ?? '')
  // The stored goal changed under an edit already in progress, so saving the typed text
  // would discard a goal this user never saw. Same irreversibility, same explicit act.
  const goalMovedUnderEdit =
    goalEdited.current && servedAtOpen !== null && (loop?.message ?? '') !== servedAtOpen
  const needsOverwriteConfirm = editsRedactedGoal || goalMovedUnderEdit
  const confirmBlocked = saving || !message.trim()
  // The confirm lives BELOW the action row, never in Save's position: swapping it in
  // where Save was let a double-click land on it, defeating the gate it exists to be.
  const confirmPending = confirmOverwrite && needsOverwriteConfirm
  const dismissRef = useRef<HTMLButtonElement | null>(null)
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)

  // Radix routes Escape, an outside click and the X through three separate hooks; ONE flag
  // backs all three, since a per-path copy leaves one path discarding silently.
  const dismissWouldDiscard = useCallback(() => {
    // ANY unsent edit, masked or not: a plain goal's typed text is lost just as
    // completely by the same gesture, and the asymmetry protected only half of it.
    // The numbers are lost by the same gesture as the text, so both are compared against
    // the values the open-edge seed used -- `||`, so a stored 0 matches the shown default.
    const numericUnsent =
      numericEdited.current &&
      !!loop &&
      (parseIdle(idleInput) !== (loop.idle_secs || 60) ||
        parseCycles(maxCyclesInput) !== (loop.max_cycles || 0))
    const unsent =
      (goalEdited.current && !!loop && message !== (loop.message ?? '')) || numericUnsent
    return confirmPending || (unsent && !escapeWarned)
  }, [confirmPending, message, loop, escapeWarned, idleInput, maxCyclesInput])

  const refuseDismissOnce = useCallback(() => {
    setConfirmOverwrite(false)
    // Unconditional: gating this on the gate being DOWN made an armed-gate dismissal cost
    // three presses, the first of which collapsed the gate and explained nothing.
    setEscapeWarned(true)
    textareaRef.current?.focus()
  }, [])

  // Disabling Save drops focus to <body>, so land it on the arm that WRITES NOTHING -- the
  // keep-stored arm still PATCHes, so a habituated second Enter there commits a partial save.
  useEffect(() => {
    if (confirmPending) dismissRef.current?.focus()
  }, [confirmPending])
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

  // Only a genuine user edit should persist a draft. Seeding from the live loop
  // or restoring a remembered draft on open must NOT re-write the store (doing
  // so would reset the slot's TTL / LRU position on a mere view, and could
  // mirror a live loop's config into the user-draft store). `hasEdited` gates
  // the persist so it fires on real onChange edits only.
  const hasEdited = useRef(false)
  // Latest field values, kept current every render so the close-flush below
  // (which runs from a stable handler) can read them.
  const latest = useRef({ slotKey, message, idleInput, maxCyclesInput, loop })
  latest.current = { slotKey, message, idleInput, maxCyclesInput, loop }

  // Compute the draft to persist for the current field state, or null to drop
  // the slot: the blank / pristine-default case stores nothing so an emptied or
  // untouched popover never pins the template. (Only reached when no loop is
  // running — a live loop is authoritative and its config is never mirrored
  // into the user-draft store; persistence is skipped entirely while a loop is
  // present.)
  function draftToPersist(s: typeof latest.current): GoalDraft | null {
    const idleSecs = parseIdle(s.idleInput)
    const maxCycles = parseCycles(s.maxCyclesInput)
    const isPristineDefault = s.message === DEFAULT_MSG && idleSecs === 60 && maxCycles === 0
    return isPristineDefault ? null : { message: s.message, idleSecs, maxCycles }
  }

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
  // authoritative source; otherwise the last per-slot draft is restored.
  // One read seeds all three fields. Runs in an effect (not render) so the
  // render itself performs no storage read/write.
  useEffect(() => {
    if (!open) return
    hasEdited.current = false
    goalEdited.current = false
    numericEdited.current = false
    // Latched HERE, at open, not at the first keystroke: a live update landing between the
    // render and that keystroke would otherwise become its own baseline and pass unnoticed.
    setRedactedAtOpen(Boolean(loop?.message_redacted))
    setServedAtOpen(loop?.message ?? '')
    setFingerprintAtOpen(loop?.message_fingerprint ?? '')
    setError('')
    // A pending confirmation must not survive a close: reopening later would
    // put a primed erase under the next press.
    setConfirmClear(false)
    // Same reason for the overwrite gate: an armed confirmation surviving a dismiss
    // would let the next Save replace a redacted goal with no fresh confirmation.
    setConfirmOverwrite(false)
    setEscapeWarned(false)
    setSaveNotice(null)
    if (loop) {
      // `||` (not `??`) is deliberate: a loop with idle_secs/max_cycles of 0
      // or an empty message shows the 60 / 0 / default template.
      setMessage(loop.message || DEFAULT_MSG)
      setIdleInput(String(loop.idle_secs || 60))
      setMaxCyclesInput(String(loop.max_cycles || 0))
    } else {
      const remembered = loadGoalDraft(slotKey)
      setMessage(remembered ? remembered.message : DEFAULT_MSG)
      setIdleInput(String(remembered ? remembered.idleSecs : 60))
      setMaxCyclesInput(String(remembered ? remembered.maxCycles : 0))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- open-edge seed only; loop/slotKey are read fresh each open
  }, [open])

  // A loop can arrive or change its settings while the popover is ALREADY open, which the
  // open-edge seed above cannot see -- and Save sends both numbers unconditionally.
  useEffect(() => {
    if (!open || !loop || numericEdited.current) return
    setIdleInput(String(loop.idle_secs || 60))
    setMaxCyclesInput(String(loop.max_cycles || 0))
  }, [open, loop])

  // Flush a pending debounced edit synchronously when the popover closes OR
  // unmounts while open, so edits within the last DRAFT_SAVE_DEBOUNCE_MS
  // window aren't lost. Effect cleanup covers both paths.
  useEffect(() => {
    if (!open) return
    return () => {
      if (!hasEdited.current || latest.current.loop) return
      saveGoalDraft(latest.current.slotKey, draftToPersist(latest.current))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- stable cleanup reading the latest ref
  }, [open])

  // Persist edits per slot, debounced with the same DRAFT_SAVE_DEBOUNCE_MS as
  // chat drafts so a long goal doesn't drive a synchronous localStorage write on
  // every keystroke. Skips until the user actually edits a field (so opening the
  // popover or the open-restore setState above never writes).
  useEffect(() => {
    if (!open || !hasEdited.current || loop) return
    const timer = setTimeout(() => saveGoalDraft(slotKey, draftToPersist(latest.current)), DRAFT_SAVE_DEBOUNCE_MS)
    return () => clearTimeout(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `draftToPersist` is a pure transform of the ref snapshot it is handed, redeclared each render, so its identity carries no information the deps above miss. Depending on it would restart the debounce timer on every unrelated re-render — the coalescing this effect exists for.
  }, [open, slotKey, message, idleInput, maxCyclesInput, loop])

  async function save(opts?: { keepStoredGoal?: boolean }) {
    if (writeDisabled) return
    // ``=== true`` on purpose: a call site that forwards a DOM event as the first
    // argument must never enable this, only an explicit caller.
    const keepStoredGoal = opts?.keepStoredGoal === true
    // The overwrite is IRREVERSIBLE and the server cannot return the original, so an
    // edit to a redacted goal needs an explicit act, not passive copy the user skims.
    if (needsOverwriteConfirm && !confirmOverwrite && !keepStoredGoal) {
      const liveFingerprint = loop?.message_fingerprint ?? ''
      // The stored goal can move BEFORE this first click, and adopting that token made
      // ``expect_fingerprint`` match server-side, so the 409 could not fire on it.
      if (fingerprintAtOpen !== null && liveFingerprint !== fingerprintAtOpen) {
        setConfirmRearmed(true)
      }
      setConfirmArmedFor(loop?.message ?? '')
      setConfirmArmedFingerprint(liveFingerprint)
      setFingerprintAtOpen(liveFingerprint)
      setConfirmOverwrite(true)
      return
    }
    // Past the gate, the stored goal must still be the one the confirm was armed on: a
    // click answering a question about text no longer there re-arms instead of committing.
    if (confirmOverwrite && !keepStoredGoal && (loop?.message ?? '') !== confirmArmedFor) {
      setConfirmArmedFor(loop?.message ?? '')
      setConfirmArmedFingerprint(loop?.message_fingerprint ?? '')
      setFingerprintAtOpen(loop?.message_fingerprint ?? '')
      setConfirmRearmed(true)
      return
    }
    setConfirmRearmed(false)
    setSaving(true)
    setError('')
    try {
      // Parse from the raw strings here (not a committed number state) so a value
      // typed and then Save-clicked without an intervening blur is still captured.
      const idle_secs = parseIdle(idleInput)
      const max_cycles = parseCycles(maxCyclesInput)
      const body = JSON.stringify({ slot_key: slotKey, message, idle_secs, max_cycles })
      // The GET that populated `loop.message` returns a SCRUBBED projection, so echoing
      // it back unconditionally would overwrite the stored message with its redaction.
      const patch: Record<string, unknown> = { idle_secs, max_cycles, active: true }
      if (loop && !keepStoredGoal && goalEdited.current && message !== (loop.message ?? '')) {
        patch.message = message
        // The baseline the confirm was ARMED on, never the live one, which carries the newer
        // goal's own token; read only while the gate is up so no latch outlives its confirm.
        const armed = confirmOverwrite ? confirmArmedFingerprint : null
        patch.expect_fingerprint = armed ?? loop.message_fingerprint ?? ''
      }
      const resp = loop
        ? await fetch(`/api/autonudge/${loop.id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch) })
        : await fetch('/api/autonudge', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body })
      const data = await resp.json()
      if (resp.status === 409) {
        // Not a failed save: the newer goal is intact and the user's view was stale.
        setConfirmOverwrite(false)
        setConfirmArmedFor(null)
        setConfirmArmedFingerprint(null)
        // REFETCH first: the message promises "save again to compare", and the compare gate
        // keys on the served goal having changed, so without this the next Save repeats it.
        // Via the SLOT route: no GET is registered for a bare loop id, so that path 405s.
        // Through the QUERY CLIENT, not a bare fetch: a bare one had no retry, so a
        // transient failure here left the promised comparison permanently stale.
        try {
          const freshData = await queryClient.fetchQuery({
            queryKey: ['autonudge-slot', slotKey],
            queryFn: async () => {
              const fresh = await fetch(`/api/autonudge/slot/${encodeURIComponent(slotKey)}`)
              if (!fresh.ok) throw new Error(`HTTP ${fresh.status}`)
              return fresh.json()
            },
            staleTime: 0,
          })
          if (freshData?.loop) onChange(freshData.loop)
        } catch {
          // SURFACED, not swallowed: the stale fingerprint makes the next Save repeat this
          // same 409, so hiding the refetch failure hides why the retry never converges.
          // WITHOUT the server's own 409 text: it tells the user to save again and compare,
          // and the failed reload is precisely what makes that comparison impossible.
          throw new Error(i18nT('components.autoNudgePopover.conflict_reload_failed'))
        }
        setError(data.error || `HTTP ${resp.status}`)
        return
      }
      if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`)
      // A 200 can still have kept the stored goal, so surface it and stay open rather
      // than reporting a save that did not fully happen.
      setConfirmOverwrite(false)
      // The patch carried both numbers on every 200, so the inputs are no longer ahead of
      // the store -- and a stale flag freezes the served-value seed for the rest of the open.
      numericEdited.current = false
      if (data.message_ignored === true) {
        setSaveNotice('ignored')
        // The confirm button the user pressed unmounts with the gate, so focus would fall
        // to <body> here too; the notice below is announced via role="status".
        textareaRef.current?.focus()
        onChange(data.loop)
        return
      }
      onChange(data.loop)
      if (keepStoredGoal) {
        // NOT `ignored`: nothing was ignored, the user declined the overwrite, and that
        // notice would blame a text match and ask for the retry they just refused.
        setSaveNotice('kept')
        // The user is still editing: closing reseeds the textarea from the served
        // projection on reopen, and no draft covers it while a loop exists.
        textareaRef.current?.focus()
        return
      }
      setSaveNotice(null)
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
      const resp = await fetch(`/api/autonudge/${loop.id}?intent=${intent}`, { method: 'DELETE' })
      if (!resp.ok) {
        // Parse JSON body for server-supplied error (e.g. 503 when feature disabled).
        // Only on error path: a successful DELETE may return 204 No Content.
        const data = await resp.json().catch(() => ({}))
        throw new Error(data.error || `HTTP ${resp.status}`)
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
        onEscapeKeyDown={e => {
          if (!dismissWouldDiscard()) return
          e.preventDefault()
          refuseDismissOnce()
        }}
        onInteractOutside={e => {
          if (!dismissWouldDiscard()) return
          e.preventDefault()
          refuseDismissOnce()
        }}
      >
        <div className="flex items-center justify-between mb-2">
          <div className="flex items-center gap-2 font-medium text-text">
            <Goal size={14} className={loop?.active ? 'text-accent' : 'text-muted'} />
            {i18nT('components.autoNudgePopover.set_a_goal')}
            {loop?.active && <span className="text-muted text-[11px]">{i18nT('components.autoNudgePopover.cycle')} {cycleText}</span>}
          </div>
          <button aria-label={i18nT('components.autoNudgePopover.close')} onClick={() => {
            if (dismissWouldDiscard()) {
              refuseDismissOnce()
              return
            }
            onOpenChange(false)
          }} className="text-muted hover:text-text bg-transparent border-none cursor-pointer">
            <X size={14} />
          </button>
        </div>
        {onBackToBoundedMonitor ? (
          <>
            <button
              type="button"
              onClick={onBackToBoundedMonitor}
              className="mb-2 inline-flex items-center gap-1 border-none bg-transparent p-0 text-[11px] text-muted cursor-pointer hover:text-text"
            >
              <ArrowLeft size={13} className="lucide-inline" aria-hidden />
              {i18nT('components.sessionAutomationPopover.back_to_bounded_monitor')}
            </button>
            <p role="note" className="mb-2 rounded-md border border-warn/30 bg-warn-subtle px-2 py-1.5 text-[11px] text-warn-fg">
              {i18nT('components.sessionAutomationPopover.legacy_notice')}
            </p>
          </>
        ) : null}
        <p className="text-muted text-[11px] mb-3 leading-relaxed">{i18nT('components.autoNudgePopover.give_the_agent_a_goal_and_it_will_keep_working_t')}</p>

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

        <div className="text-muted text-[11px] mb-1">{i18nT('components.autoNudgePopover.goal_description')}</div>
        {loop?.message_redacted && (
          <div role="status" data-testid="autonudge-redacted-notice" className="text-warn text-[12px] font-medium mb-1">
            {/* One line per sentence, at body size: the irreversibility warning read as
                fine print and sat mid-paragraph where a skimming reader missed it. Split
                on the terminator so bn/hi danda and ja/zh ideographic stop work too. */}
            {i18nT('components.autoNudgePopover.message_redacted_notice')
              .split(/(?<=[.।。])\s+/)
              .filter(Boolean)
              .map((sentence, i) => (
                <div key={i} className={i === 0 ? '' : 'mt-1'}>
                  {sentence}
                </div>
              ))}
          </div>
        )}
        <textarea
          ref={textareaRef}
          aria-label={i18nT('components.autoNudgePopover.goal_description')}
          value={message}
          disabled={writeDisabled}
          onChange={e => {
            hasEdited.current = true
            goalEdited.current = true
            // A fresh edit re-arms the discard warning: the text at stake is not the text
            // the previous warning was about.
            setEscapeWarned(false)
            // An armed confirmation answers the text it was armed FOR. Reverting to the
            // redacted copy and editing again reused it, overwriting with no second ask.
            setConfirmOverwrite(false)
            setSaveNotice(null)
            setMessage(e.target.value)
          }}
          rows={6}
          className="w-full bg-bg border border-border rounded p-2 text-[12px] font-mono resize-y mb-3 text-text"
          placeholder={i18nT('components.autoNudgePopover.describe_what_you_want_the_agent_to_accomplish')}
        />

        {escapeWarned && (
          <div role="status" data-testid="autonudge-escape-warned" className="text-warn text-[12px] font-medium mb-2">
            {i18nT('components.autoNudgePopover.escape_discards_notice')}
          </div>
        )}

        <div className="flex flex-col gap-3 mb-3 sm:flex-row">
          <div className="flex-1">
            <div className="text-muted text-[11px] mb-1">{i18nT('components.autoNudgePopover.seconds_between_nudges')}</div>
            <input
              type="number"
              aria-label={i18nT('components.autoNudgePopover.seconds_between_nudges')}
              min={15}
              max={86400}
              value={idleInput}
              disabled={writeDisabled}
              onChange={e => {
                hasEdited.current = true
                numericEdited.current = true
                // A fresh edit re-arms the discard warning, exactly as the goal textarea
                // does: the value at stake is not the one the previous warning was about.
                setEscapeWarned(false)
                setIdleInput(e.target.value)
              }}
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
              value={maxCyclesInput}
              disabled={writeDisabled}
              onChange={e => {
                hasEdited.current = true
                numericEdited.current = true
                setEscapeWarned(false)
                setMaxCyclesInput(e.target.value)
              }}
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

        {saveNotice === 'ignored' && (
          <div role="status" data-testid="autonudge-ignored-fields" className="text-warn text-[11px] mb-2">
            {i18nT('components.autoNudgePopover.ignored_fields_notice')}
          </div>
        )}

        {saveNotice === 'kept' && (
          <div role="status" data-testid="autonudge-kept-stored-goal" className="text-muted text-[11px] mb-2">
            {i18nT('components.autoNudgePopover.kept_stored_goal_notice')}
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
              <Btn
                type="button"
                danger
                onClick={() => {
                  // The two gates are mutually exclusive: an armed overwrite
                  // beside this erase puts two destructive asks on one screen.
                  setConfirmOverwrite(false)
                  setConfirmClear(true)
                }}
                disabled={saving}
              >
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
              onClick={() => save()}
              disabled={saving || writeDisabled || !message.trim() || confirmPending}
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
        {confirmPending && !confirmClear && (
          <div className="flex flex-col items-end gap-2 mt-2">
            {confirmRearmed && (
              <p
                role="status"
                data-testid="autonudge-confirm-rearmed"
                className="m-0 text-warn text-[12px] font-medium"
              >
                {i18nT('components.autoNudgePopover.confirm_rearmed_notice')}
              </p>
            )}
            <p role="status" data-testid="autonudge-confirm-question" className="m-0 text-warn text-[12px]">
              {/* Moved wins when BOTH hold: only this arm names a goal the user has never
                  read, and the redacted arm's amber notice above still supplies its why. */}
              {i18nT(
                goalMovedUnderEdit
                  ? 'components.autoNudgePopover.confirm_overwrite_moved_question'
                  : 'components.autoNudgePopover.confirm_overwrite_plain_question'
              )}
            </p>
            {goalMovedUnderEdit && (
              // The choice is irreversible and the newer text is in hand here, so show it
              // rather than asking the user to discard something they have never read.
              <p
                data-testid="autonudge-moved-goal-preview"
                // It scrolls, so with no tab stop a keyboard reader cannot reach the tail.
                // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- see above
                tabIndex={0}
                role="region"
                aria-label={i18nT('components.autoNudgePopover.moved_goal_preview_region')}
                className="m-0 max-w-full self-stretch text-text text-[12px] break-words opacity-80 max-h-32 overflow-y-auto"
              >
                {/* LABELLED: unlabelled text read as the user's own pending edit rather than
                    the stored goal being discarded. One key, so a translator can reorder it. */}
                {i18nT('components.autoNudgePopover.moved_goal_preview', {
                  // Shown IN FULL and scrolled rather than truncated: the click authorises
                  // destroying all of it, so an elided tail hides part of the decision.
                  goal: confirmArmedFor ?? '',
                })}
                {loop?.message_redacted && (
                  // The preview is the SERVED projection, so this arm can ask the user to
                  // discard a goal they were shown only as a mask. Say so at the decision.
                  <span className="block opacity-80">
                    {i18nT('components.autoNudgePopover.moved_goal_preview_redacted')}
                  </span>
                )}
              </p>
            )}
            {/* Column unconditionally: the breakpoint keys on the VIEWPORT, but this row
                lives in a fixed 420px popover, so a wide screen wrapped long labels. */}
            <div className="flex flex-col gap-2 w-full">
              <button
                ref={dismissRef}
                data-testid="autonudge-dismiss-overwrite"
                onClick={() => {
                  // The WRITE-FREE exit: both other buttons PATCH, and the only silent
                  // dismissal was Escape, which DISCARDS the typed goal.
                  setConfirmOverwrite(false)
                  textareaRef.current?.focus()
                }}
                onKeyDown={e => {
                  if ((e.key === 'Enter' || e.key === ' ') && e.repeat) e.preventDefault()
                }}
                // GHOST, not filled: this is the only arm that writes NOTHING, so it must
                // not share a shape with the arm that saves the other settings.
                className="px-3 py-1 rounded bg-transparent text-muted border-none underline cursor-pointer hover:text-text"
              >
                {i18nT('components.autoNudgePopover.dismiss_without_saving')}
              </button>
              <button
                data-testid="autonudge-decline-overwrite"
                onClick={() => {
                  // Dismiss the gate ONLY. Restoring the served text here discarded the
                  // user's typed goal, which no draft covers while a loop exists.
                  setConfirmOverwrite(false)
                  // This button unmounts with the gate, so focus would fall to <body> on
                  // the gate's own SAFE path. Land it on the text the user was editing.
                  textareaRef.current?.focus()
                  // Answers the GOAL question, not the whole form: persist the other
                  // settings so a changed interval is not silently dropped.
                  save({ keepStoredGoal: true })
                }}
                onKeyDown={e => {
                  // Same guard as the overwrite button: this one is focused on mount, so
                  // a repeating Enter from Save would otherwise dismiss the gate unseen.
                  if ((e.key === 'Enter' || e.key === ' ') && e.repeat) e.preventDefault()
                }}
                // OUTLINED: it does write (the other settings), so it is not the ghost
                // arm, and it is not destructive, so it is not the filled warn arm.
                className="px-3 py-1 rounded bg-card text-text border border-border cursor-pointer hover:opacity-90"
              >
                {/* On a PAUSED loop this arm also resumes it, so it must say so --
                    the same split base Save makes between save and start_loop. */}
                {loop?.active
                  ? i18nT('components.autoNudgePopover.keep_original_goal')
                  : i18nT('components.autoNudgePopover.keep_original_goal_start')}
              </button>
              {/* Rule between the two DECLINE answers above and the destructive one below,
                  so a yes/no reader cannot take a partial-save button for a plain cancel. */}
              <div className="border-t border-border" aria-hidden="true" />
              <button
                data-testid="autonudge-confirm-overwrite"
                onClick={() => {
                  if (confirmBlocked) return
                  save()
                }}
                onKeyDown={e => {
                  if ((e.key === 'Enter' || e.key === ' ') && e.repeat) e.preventDefault()
                }}
                // Truly disabled, not aria-disabled: an empty goal made this button swallow
                // the click silently, which reads as broken. The title names the reason.
                disabled={confirmBlocked}
                className={`px-3 py-1 rounded bg-warn text-warn-fg border-none cursor-pointer hover:bg-warn/90 ${confirmBlocked ? 'opacity-50' : ''}`}
              >
                {i18nT(
                  goalMovedUnderEdit
                    ? 'components.autoNudgePopover.confirm_overwrite_moved'
                    : 'components.autoNudgePopover.confirm_overwrite_masked'
                )}
              </button>
            </div>
            {confirmBlocked && !message.trim() && (
              // VISIBLE, not just the title: a tooltip is unreachable on touch and by
              // keyboard, so the only explanation for a dead button never arrived.
              <p
                data-testid="autonudge-overwrite-blocked-reason"
                role="status"
                className="m-0 text-muted text-[11px]"
              >
                {i18nT('components.autoNudgePopover.overwrite_blocked_reason')}
              </p>
            )}
          </div>
        )}
      </PopoverContent>}
    </Popover>
  )
}
