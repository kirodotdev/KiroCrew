import { type ReactNode, useEffect, useId, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, Goal, Pause, Play, Radar, X, Zap } from 'lucide-react'
import { Popover, PopoverTrigger, PopoverContent } from './ui/popover'
import { Btn } from './ui'
import ErrorNotice from './ErrorNotice'
import { cronJobsQuery } from '../api/cronJobsQuery'
import { runBelongsToSlot } from '../apps/workflows/runModel'
import { loadGoalDraft, saveGoalDraft, type GoalDraft } from '../utils/goalDrafts'
import { DRAFT_SAVE_DEBOUNCE_MS } from '../utils/draftConstants'

import { i18nT } from '../i18n/t'
import { fmtTimeNumeric } from '../i18n/format'
import { type AutoNudgeLoop, cycleText as loopCycleText, nextCycleText, judgeReading, judgeVerdictTime, loopFinished, PULL_REQUEST_WATCH_KINDS, AUTONUDGE_LOOPS_QUERY_KEY } from './autoNudgeLoop'
export type { AutoNudgeLoop } from './autoNudgeLoop'

interface Props {
  slotKey: string
  loop: AutoNudgeLoop | null
  open: boolean
  onOpenChange: (open: boolean) => void
  /** A write landed: the record as the server returned it, or null for a removal. */
  onChange: (loop: AutoNudgeLoop | null) => void
  /** A fire landed: the loop's next cycle is armed to run now. Carries the
   *  record the fire was PRESSED on (the written record when a write preceded
   *  the fire, else the loop this editor held), never the route's answer: that
   *  is the live loop serialized after the route's audit await and can already
   *  carry the delivery. The parent arms the deadline on the record IT holds. */
  onFired: (fired: AutoNudgeLoop) => void
  /** Present when this editor is the popover's default view and a bounded monitor can still be armed. */
  onSetUpBoundedMonitor?: () => void
  /** Disable legacy-loop writes while leaving Clear available for stale state. Also renders the reason. */
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
 * explains what the token becomes (#10458). A goal typed with the token still
 * works; the default goal (`defaultMsg`) names the `autonudge_stop` tool instead.
 */
export const STOP_FILE_TOKEN = '{{STOP_FILE}}'

/** The pre-filled goal, read per call so it follows the active language. The
 *  file and tool names are interpolated so no translation or pseudolocale can
 *  rewrite them: the agent must receive them verbatim. */
const defaultMsg = () => i18nT('components.autoNudgePopover.default_goal', {
  northStar: 'north_star.md',
  roadmap: 'roadmap.md',
  tasks: 'tasks.md',
  stopTool: 'autonudge_stop',
})

/** The paused line per persisted `stopped_reason` (`autoNudgeLoop.ts` lists
 *  the codes). An unlisted or absent reason renders the bare "Paused". */
const PAUSED_STATUS_KEY: Record<string, string> = {
  manual: 'components.autoNudgePopover.paused_manual',
  autonudge_stop: 'components.autoNudgePopover.paused_agent',
  cycle_cap: 'components.autoNudgePopover.paused_cycle_cap',
  runtime_budget: 'components.autoNudgePopover.paused_runtime_budget',
  approval_stalled: 'components.autoNudgePopover.paused_approval_stalled',
  structural_terminal: 'components.autoNudgePopover.paused_last_cycle_failed',
  session_start_failures: 'components.autoNudgePopover.paused_last_cycle_failed',
}

/** The three editable fields, as every write of the form sends them. */
type LoopFields = { message: string; idle_secs: number; max_cycles: number }

/** One armed script cron owned by this chat slot. */
interface SlotWatch {
  id: string
  name: string
  schedule: string
  next_run_ts: number | null
}

export default function AutoNudgePopover({ slotKey, loop, open, onOpenChange, onChange, onFired, onSetUpBoundedMonitor, writeDisabled = false, interrupted = false, trigger, content }: Props) {
  // `||` (not `??`) is deliberate on the loop tier: it preserves the fallback
  // so a loop with idle_secs/max_cycles of 0 or an empty message still shows
  // the 60 / 0 / default template rather than a bare 0 / "".
  const [message, setMessage] = useState(() => loop?.message || defaultMsg())
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

  const parseIdle = (s: string) => parseInt(s, 10) || 60
  const parseCycles = (s: string) => parseInt(s, 10) || 0

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
  /* What the fields held when the popover last SHOWED them: the record they
     were seeded from on open, or what the user's last write sent. `editedFields`
     measures against this rather than the live record, because the fields
     never re-sync while open: a revision another writer landed meanwhile is not
     in the form, and measured against it an untouched field would read as an
     edit and write the stale value back over it. Seeded on the first render
     because Play's name renders off it before the open-edge effect runs. */
  const seeded = useRef<LoopFields | null>(
    loop ? { message: loop.message || defaultMsg(), idle_secs: loop.idle_secs || 60, max_cycles: loop.max_cycles || 0 } : null,
  )

  // Compute the draft to persist for the current field state, or null to drop
  // the slot: the blank / pristine-default case stores nothing so an emptied or
  // untouched popover never pins the template. (Only reached when no loop is
  // running — a live loop is authoritative and its config is never mirrored
  // into the user-draft store; persistence is skipped entirely while a loop is
  // present.)
  function draftToPersist(s: typeof latest.current): GoalDraft | null {
    const idleSecs = parseIdle(s.idleInput)
    const maxCycles = parseCycles(s.maxCyclesInput)
    const isPristineDefault = s.message === defaultMsg() && idleSecs === 60 && maxCycles === 0
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
    setError('')
    // A pending confirmation must not survive a close: reopening later would
    // put a primed erase under the next press.
    setConfirmClear(false)
    if (loop) {
      // `||` (not `??`) is deliberate: a loop with idle_secs/max_cycles of 0
      // or an empty message shows the 60 / 0 / default template.
      setMessage(loop.message || defaultMsg())
      setIdleInput(String(loop.idle_secs || 60))
      setMaxCyclesInput(String(loop.max_cycles || 0))
      seeded.current = { message: loop.message || defaultMsg(), idle_secs: loop.idle_secs || 60, max_cycles: loop.max_cycles || 0 }
    } else {
      const remembered = loadGoalDraft(slotKey)
      setMessage(remembered ? remembered.message : defaultMsg())
      setIdleInput(String(remembered ? remembered.idleSecs : 60))
      setMaxCyclesInput(String(remembered ? remembered.maxCycles : 0))
      seeded.current = remembered
        ? { message: remembered.message, idle_secs: remembered.idleSecs, max_cycles: remembered.maxCycles }
        : { message: defaultMsg(), idle_secs: 60, max_cycles: 0 }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- open-edge seed only; loop/slotKey are read fresh each open
  }, [open])

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

  /** The fields as the form holds them, parsed from the raw strings so a value
   *  typed and pressed without an intervening blur is still captured. */
  function formFields(): LoopFields {
    return { message, idle_secs: parseIdle(idleInput), max_cycles: parseCycles(maxCyclesInput) }
  }

  /** The fields the user changed since the form last showed them (`seeded`),
   *  and ONLY those; null when nothing changed. Compared on the PARSED values,
   *  so a blur that normalised "090" to "90" is not an edit. Never seeded reads
   *  as all edited: with no baseline to prove the form untouched, sending it is
   *  the safe default. */
  function editedFields(): Partial<LoopFields> | null {
    const base = seeded.current
    const now = formFields()
    if (!base) return now
    const edited: Partial<LoopFields> = {}
    if (now.message !== base.message) edited.message = now.message
    if (now.idle_secs !== base.idle_secs) edited.idle_secs = now.idle_secs
    if (now.max_cycles !== base.max_cycles) edited.max_cycles = now.max_cycles
    return Object.keys(edited).length ? edited : null
  }
  const formDirty = editedFields() !== null

  /** DONE: a finished loop -- the agent created its stop file, or the watched
   *  pull request merged or closed. Not a kind of paused: the service refuses to
   *  revive it, so this surface offers nothing to press and the fields go read-only. */
  const finished = loopFinished(loop)

  const pauseName = i18nT('components.autoNudgePopover.pause_loop')
  /** The fire control names what THIS press does: with no loop it creates and
   *  starts the loop (no fire); on a loop it fires now, resuming first when the
   *  loop is paused and saving first when the form is dirty. */
  const playName = !loop
    ? i18nT('components.autoNudgePopover.start_loop')
    : loop.active
      ? i18nT(formDirty ? 'components.autoNudgePopover.trigger_nudge' : 'components.autoNudgePopover.nudge_now')
      : i18nT(formDirty ? 'components.autoNudgePopover.save_edits_and_resume' : 'components.autoNudgePopover.resume_loop')
  /** An icon-only `Btn` is square: `twMerge` lets `p-1.5` replace the text
   *  button's `px-2.5 py-1`. `relative` anchors Play's dirty dot. */
  const ICON_BTN = 'relative p-1.5'
  /** Appended to each field's classes once the loop is finished: read-only and
   *  dimmed, values kept. A string rather than `disabled:` variants so the
   *  crew/member capability note keeps its own undimmed disabled look. */
  const FINISHED_FIELD = finished ? ' opacity-60' : ''

  const JSON_HEADERS = { 'Content-Type': 'application/json' }

  /** Every write to a loop (PATCH, POST create, POST fire) goes through this
   *  one mutation so a success invalidates the shared loops query: the
   *  full-registry readers (the Crew Members patrol block) must never keep a
   *  stale copy of a record a write just changed. */
  const loopWrite = useMutation({
    mutationFn: async ({ url, method, body }: { url: string, method: 'PATCH' | 'POST', body?: unknown }): Promise<AutoNudgeLoop> => {
      const resp = await fetch(url, body === undefined
        ? { method }
        : { method, headers: JSON_HEADERS, body: JSON.stringify(body) })
      const data = await resp.json().catch(() => ({}))
      if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`)
      return data.loop
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: AUTONUDGE_LOOPS_QUERY_KEY })
    },
  })

  /** Remove a paused loop's record for good; the only erase on this surface.
   *  Invalidates the shared loops query like every other write here, so the
   *  full-registry readers do not keep the row through a websocket outage. */
  async function clear() {
    if (!loop) return
    setSaving(true)
    try {
      const resp = await fetch(`/api/autonudge/${loop.id}?intent=clear`, { method: 'DELETE' })
      if (!resp.ok) {
        // A successful DELETE may return 204 No Content, so the body is read only here.
        const data = await resp.json().catch(() => ({}))
        throw new Error(data.error || `HTTP ${resp.status}`)
      }
      void queryClient.invalidateQueries({ queryKey: AUTONUDGE_LOOPS_QUERY_KEY })
      onChange(null)
      onOpenChange(false)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  /** Pause in place: `PATCH active:false`, which the service records as
   *  `stopped_reason: "manual"` on a running loop (a pause reaching a loop a
   *  bound already stopped keeps the bound, and the record handed up says so).
   *  The body carries ONLY `active`: a pause is not a save. Stays open, like
   *  every control whose outcome is visible in place. */
  async function pause() {
    if (!loop || writeDisabled) return
    setSaving(true)
    setError('')
    try {
      onChange(await loopWrite.mutateAsync({ url: `/api/autonudge/${loop.id}`, method: 'PATCH', body: { active: false } }))
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  /** A press that writes and then fires. The two legs are NOT a transaction:
   *  a refused write fires nothing, and a write that lands followed by a
   *  refused fire (409 while a turn is in flight, mid-fire, or paused by another
   *  tab in between) leaves the loop written with the refusal shown inline --
   *  rolling it back would turn a refused shortcut into an undone edit. Stays
   *  open: the outcome is visible in place and a refusal needs somewhere to
   *  land. */
  async function runControl(sequence: () => Promise<void>) {
    if (writeDisabled) return
    setSaving(true)
    setError('')
    try {
      await sequence()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  /** The write leg: hands the record up the moment it lands (a written record
   *  held back until the fire settled was handed up stale, over a frame that
   *  had moved the loop on). The fields that went out rejoin the pristine
   *  baseline as the server STORED them (`adoptStored`); fields not sent keep
   *  their baseline, since the record may hold another writer's value for them. */
  async function writeLoop(write: { url: string, method: 'PATCH' | 'POST', body: unknown }, fieldsSent: Partial<LoopFields> | null): Promise<AutoNudgeLoop> {
    const written = await loopWrite.mutateAsync(write)
    if (fieldsSent) adoptStored(written, fieldsSent)
    onChange(written)
    return written
  }

  /** The service clamps what it stores (interval floor and ceiling, cap >= 0),
   *  so seeding the baseline from the SENT values would leave an interval typed
   *  as 1 reading pristine over a record running at 15. A response without the
   *  record (a legacy stub) falls back to the sent values. */
  function adoptStored(written: AutoNudgeLoop | null | undefined, fieldsSent: Partial<LoopFields>) {
    const next = { ...(seeded.current ?? formFields()) }
    if ('message' in fieldsSent) {
      next.message = written ? written.message || defaultMsg() : fieldsSent.message ?? next.message
      setMessage(next.message)
    }
    if ('idle_secs' in fieldsSent) {
      next.idle_secs = written ? written.idle_secs || 60 : fieldsSent.idle_secs ?? next.idle_secs
      setIdleInput(String(next.idle_secs))
    }
    if ('max_cycles' in fieldsSent) {
      next.max_cycles = written ? written.max_cycles || 0 : fieldsSent.max_cycles ?? next.max_cycles
      setMaxCyclesInput(String(next.max_cycles))
    }
    seeded.current = next
  }

  /** The fire leg. Sends NO body (the nudge fired is whatever the loop holds,
   *  read server-side) and hands up NO record: it reports the PRESS, whose
   *  `last_fire_ts` is a pre-request baseline, never the route's answer, which
   *  can already carry the delivery (see `onFired`). */
  async function fireNow(pressed: AutoNudgeLoop) {
    await loopWrite.mutateAsync({ url: `/api/autonudge/${pressed.id}/fire`, method: 'POST' })
    onFired(pressed)
  }

  /** Play on a loop: save the edited fields (only those -- an untouched field
   *  is never written back over a revision that landed while the popover sat
   *  open), resume when paused, then fire. The write comes first because
   *  `fire_now` fires whatever the loop holds and refuses an inactive loop.
   *  Server-side the resume resets only the counter behind a spent bound (a
   *  spent cycle cap restarts the count, a spent time budget restarts the
   *  clock), so no bound has to be raised and the rest of the loop resumes
   *  from the cycle it stopped at. */
  function play() {
    if (!loop) return
    const fields = editedFields()
    const body = loop.active ? fields : { ...fields, active: true }
    return runControl(async () => {
      const written = body ? await writeLoop({ url: `/api/autonudge/${loop.id}`, method: 'PATCH', body }, fields) : null
      // A legacy stub without the record's id has nothing to fire on.
      await fireNow(written?.id ? written : loop)
    })
  }

  /** Play with no loop: create and start it from the form, NO fire (product
   *  owner, 2026-10-01): the first nudge goes out after `idle_secs`, or when
   *  the user presses Play on the running loop. The write's hand-off re-keys
   *  the popover onto the created id; a refused create lands in this notice. */
  function startNow() {
    const fields = formFields()
    return runControl(async () => {
      await writeLoop(
        { url: '/api/autonudge', method: 'POST', body: { slot_key: slotKey, ...fields } },
        fields,
      )
    })
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
  const cycleText = loopCycleText(loop)
  /** Which of the finishes, and whether it earns a check mark: a goal the agent
   *  declared reached, a merge and an accepted work ledger do; a pull request
   *  closed without merging ended on a question (reopen or abandon), as did a
   *  rejected ledger, so those do not. The merged-vs-closed reading is the settled
   *  `monitor_outcome` the frame carries, never prose; a finished watch whose
   *  subject is not a pull or merge request (`monitor_kind`) is worded as a
   *  subject, because its finish is neither a merge nor a close; its two
   *  outcomes get two sentences, not one sentence and a glyph, because a
   *  screen reader hears the sentence and not the glyph. */
  const doneLine = !finished
    ? null
    : loop?.stopped_reason === 'stop_sentinel'
      ? { key: 'components.autoNudgePopover.done_stop_file', check: true }
      : !PULL_REQUEST_WATCH_KINDS.has(loop?.monitor_kind ?? '')
        ? loop?.monitor_outcome === 'success'
          ? { key: 'components.autoNudgePopover.done_subject_finished', check: true }
          : { key: 'components.autoNudgePopover.done_subject_ended', check: false }
        : loop?.monitor_outcome === 'success'
          ? { key: 'components.autoNudgePopover.done_pr_merged', check: true }
          : { key: 'components.autoNudgePopover.done_pr_closed', check: false }
  /** THE STATUS LINE under the title, independent of the controls: a running
   *  loop's countdown, why a finished one is done, or why a paused one is paused. */
  const statusText = loop
    ? loop.active
      ? countdownText
      : doneLine
        ? i18nT(doneLine.key)
        : loop.stopped_reason && loop.stopped_reason in PAUSED_STATUS_KEY
          ? i18nT(PAUSED_STATUS_KEY[loop.stopped_reason], { cycles: loop.cycle_count, max: loop.max_cycles })
          : i18nT('components.autoNudgePopover.loop_paused')
    : ''
  /** The title names the state: the goal's cycle while running, Done once
   *  finished, Paused otherwise, and the invitation when there is no loop. */
  const titleText = !loop
    ? i18nT('components.autoNudgePopover.set_a_goal')
    : loop.active
      ? i18nT('components.autoNudgePopover.goal_active_cycle', { cycle: cycleText })
      : finished
        ? i18nT('components.autoNudgePopover.loop_done')
        : i18nT('components.autoNudgePopover.loop_paused')
  /** Whether a cycle is ALREADY armed to run. Derived from the same countdown
   *  the status line renders, so the button and the text can never disagree. */
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

  const judge = judgeReading(loop)
  // A localized word per verdict outcome. The backend's token is a stable
  // identifier in a line every armed-loop owner reads, and an owner reading a
  // localized sentence should not meet an English identifier inside it. Keyed by
  // the kernel's four-value outcome set, with a word for the record's own
  // "unknown" so an unmapped token still reads as a word.
  const JUDGE_OUTCOME_WORD: Record<string, string> = {
    quiet: i18nT('components.autoNudgePopover.judge_outcome_quiet'),
    wake: i18nT('components.autoNudgePopover.judge_outcome_wake'),
    terminal: i18nT('components.autoNudgePopover.judge_outcome_terminal'),
    fallback: i18nT('components.autoNudgePopover.judge_outcome_fallback'),
  }
  const judgeOutcomeWord = (outcome: string) =>
    JUDGE_OUTCOME_WORD[outcome] ?? i18nT('components.autoNudgePopover.judge_outcome_unknown')

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
          <div className="flex items-center gap-2 font-medium text-text" data-testid="auto-nudge-title">
            <Goal size={14} className={loop?.active ? 'text-accent' : 'text-muted'} />
            {titleText}
          </div>
          <button aria-label={i18nT('components.autoNudgePopover.close')} title={i18nT('components.autoNudgePopover.close')} onClick={() => onOpenChange(false)} className="text-muted hover:text-text bg-transparent border-none cursor-pointer">
            <X size={14} />
          </button>
        </div>
        {loop && (
          /* Boxed in the state's tone (ok while running, info once finished, warn
             while paused) so the state reads before the form does -- except while
             writes are disabled, where a green box beside the capability note would
             assert a fire this session cannot act on; the countdown then stays in the
             note's neutral tone. Not an aria-live region: a running loop's line
             ticks every second and would re-announce itself to a screen reader
             each time. */
          <p
            data-testid="auto-nudge-status"
            className={`mb-2 rounded-md border px-2 py-1.5 text-[11px] leading-relaxed ${
              loop.active
                ? writeDisabled ? 'border-border bg-bg text-muted' : 'border-ok/30 bg-ok-subtle text-ok-fg'
                : finished
                  ? 'border-info/30 bg-info-subtle text-info'
                  : 'border-warn/30 bg-warn-subtle text-warn-fg'
            }`}
          >
            {doneLine?.check && <Check size={12} className="lucide-inline mr-1" aria-hidden data-testid="auto-nudge-done-check" />}
            {statusText}
          </p>
        )}
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
        {/* Once finished the three fields are read-only and dimmed, values kept:
            Done is a dead end, so there is nothing an edit could be saved into. */}
        <textarea
          aria-label={i18nT('components.autoNudgePopover.goal_description')}
          value={message}
          disabled={writeDisabled || finished}
          onChange={e => { hasEdited.current = true; setMessage(e.target.value) }}
          rows={6}
          className={`w-full bg-bg border border-border rounded p-2 text-[12px] font-mono resize-y mb-3 text-text${FINISHED_FIELD}`}
          placeholder={i18nT('components.autoNudgePopover.describe_what_you_want_the_agent_to_accomplish')}
          aria-describedby={stopFileHelp ? stopFileHelpId : undefined}
        />
        {stopFileHelp ? (
          /* Display-only explanation of the raw token above (#10458). The
             textarea keeps `{{STOP_FILE}}` because the server substitutes it
             when each nudge is sent; only the human reading the form needed
             telling what it turns into. Shown while the goal text carries the
             token, so a custom goal without it gets no orphan help line. The
             empty-sentinel arm reads the ARMED loop's record: a loop that
             carries an explicitly empty `stop_sentinel_path` has nothing to
             substitute, so the honest line is that the token goes out blank.
             The path itself is never rendered: the websocket frame withholds
             it and this surface has no owner gate. */
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
              min={15}
              max={86400}
              value={idleInput}
              disabled={writeDisabled || finished}
              onChange={e => { hasEdited.current = true; setIdleInput(e.target.value) }}
              onBlur={() => setIdleInput(String(parseIdle(idleInput)))}
              className={`w-full bg-bg border border-border rounded px-2 py-1 text-[12px] text-text${FINISHED_FIELD}`}
            />
          </div>
          <div className="flex-1">
            <div className="text-muted text-[11px] mb-1">{i18nT('components.autoNudgePopover.max_cycles_0')}</div>
            <input
              type="number"
              aria-label={i18nT('components.autoNudgePopover.max_cycles_0_infinite')}
              min={0}
              value={maxCyclesInput}
              disabled={writeDisabled || finished}
              onChange={e => { hasEdited.current = true; setMaxCyclesInput(e.target.value) }}
              onBlur={() => setMaxCyclesInput(String(parseCycles(maxCyclesInput)))}
              className={`w-full bg-bg border border-border rounded px-2 py-1 text-[12px] text-text${FINISHED_FIELD}`}
            />
          </div>
        </div>

        {/* The schedule line: the last fire, and the judge's reading under it.
            The state itself (countdown, paused reason) lives on the status
            line by the title. */}
        {loop && (
          <div className="flex flex-col items-start gap-1 mb-3" data-testid="auto-nudge-schedule">
            <div className="text-muted text-[11px]">
              {i18nT('components.autoNudgePopover.last_fire')} {loop.last_fire_ts ? fmtTimeNumeric(loop.last_fire_ts) : i18nT('components.autoNudgePopover.never')}
            </div>
            {/* The judge's own line, under the schedule it modifies. Drawn only for a
                loop that carries a brief, so a plain timer gains no row. The verdict
                half is omitted until one exists: "no verdict yet" is a different
                statement from a quiet answer, and reading a fresh judge as quiet
                would say a tick was skipped that never happened. What it shows is
                the outcome, the item COUNT and the time -- never the evidence, and
                never a probability, which lives in the decisions log where the
                thresholds are tuned. */}
            {/* ``break-words`` because the criterion is the owner's own sentence and may hold a
                token with no spaces in it -- a URL, a sha, a pasted blob. Without it such a
                criterion does not wrap, it OVERFLOWS the popover horizontally. Wrapping
                rather than clamping: the row shows the whole criterion on purpose, so the
                owner can confirm what they armed. */}
            {judge.kind === 'armed' && (
              <div className="text-muted text-[11px] break-words" data-testid="judge-line">
                {i18nT(
                  judge.sense === 'wake'
                    ? 'components.autoNudgePopover.judge_wake_when'
                    : 'components.autoNudgePopover.judge_quiet_while',
                  { criterion: judge.criterion },
                )}
                {judge.verdict ? (
                  <span>
                    {' · '}
                    {/* Two spellings because the time is the only segment that can be
                        absent: a record with no timestamp renders no clock reading, and
                        one interpolated string would leave its separator hanging with
                        nothing after it. A conditional inside the string is not
                        available to a translator, so the choice is made here. */}
                    {judgeVerdictTime(judge.verdict.at)
                      ? i18nT('components.autoNudgePopover.judge_verdict', {
                        outcome: judgeOutcomeWord(judge.verdict.outcome),
                        count: judge.verdict.items,
                        time: judgeVerdictTime(judge.verdict.at),
                      })
                      : i18nT('components.autoNudgePopover.judge_verdict_untimed', {
                        outcome: judgeOutcomeWord(judge.verdict.outcome),
                        count: judge.verdict.items,
                      })}
                  </span>
                ) : (
                  <span> · {i18nT('components.autoNudgePopover.judge_no_verdict')}</span>
                )}
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

        {/* THE ACTION ROW (product owner, 2026-10-01). While a loop exists it is
            one row: Clear stopped goal as a text link at the left on a paused
            loop (the ONLY way to delete a goal; no button chrome, so the
            two-buttons-per-row rule counts the two icons), and exactly two
            icon-only controls at the right in every state, Pause and the fire
            control, names in `aria-label` and `title`. Running: a LIGHTNING
            glyph -- a play triangle beside "Goal active" had no cold reading --
            saves pending edits and fires now. Paused: Pause is disabled and a
            PLAY glyph saves pending edits, resumes and fires -- one request
            path, nothing to raise first: a spent cycle cap restarts the
            count and a spent time budget restarts the clock, each on its
            own, and everything else resumes from the cycle it stopped at. A
            pending edit marks the fire control with a dot. The
            Clear confirm is a sub-step, not a state: it REPLACES the row with an
            accented box (the question, then a filled Clear and a plain Cancel)
            under the status line, which stays. Clear writes nothing to the
            record, so it stays live while writes are disabled: stale state must
            stay clearable. No loop: Play alone creates and starts the loop. */}
        {loop && confirmClear ? (
          <div className="flex items-center justify-between gap-2 rounded-md border border-danger px-2 py-1.5" data-testid="auto-nudge-actions">
            <span data-testid="auto-nudge-clear-question" className="text-[12px] font-medium text-text">
              {i18nT('components.autoNudgePopover.clear_goal_question')}
            </span>
            <div className="flex items-center gap-2 shrink-0" data-testid="auto-nudge-controls">
              <Btn type="button" danger className="bg-danger text-danger-fg border-danger hover:bg-danger hover:border-danger hover:opacity-90" onClick={clear} disabled={saving}>
                {i18nT('components.autoNudgePopover.clear_goal_for_good')}
              </Btn>
              <Btn type="button" onClick={() => setConfirmClear(false)} disabled={saving}>
                {i18nT('components.autoNudgePopover.cancel')}
              </Btn>
            </div>
          </div>
        ) : loop ? (
          <div className="flex items-center justify-between gap-2" data-testid="auto-nudge-actions">
            {loop.active ? <span /> : (
              <button
                type="button"
                data-testid="auto-nudge-clear"
                onClick={() => setConfirmClear(true)}
                disabled={saving}
                className="border-none bg-transparent p-0 text-[11px] text-danger underline cursor-pointer hover:text-danger disabled:opacity-30 disabled:cursor-not-allowed"
              >
                {i18nT(finished ? 'components.autoNudgePopover.clear_finished_goal' : 'components.autoNudgePopover.clear_stopped_goal')}
              </button>
            )}
            {/* DONE keeps the row's shape and kills both controls: the service
                would refuse the resume anyway, and a live Play here would promise
                one. Clear is the only way on; a new goal is set after it. */}
            <div className="flex items-center gap-2" data-testid="auto-nudge-controls">
              <Btn type="button" className={ICON_BTN} onClick={pause} disabled={saving || writeDisabled || !loop.active} aria-label={pauseName} title={pauseName}>
                <Pause size={14} aria-hidden />
              </Btn>
              <Btn
                type="button"
                primary
                className={ICON_BTN}
                onClick={play}
                /* Disabled once a cycle is already due and the form is pristine,
                   so a press visibly acknowledges itself: a second press could
                   only 409 or do nothing. A dirty form keeps Play live -- it is
                   the only save path while a loop exists, and "due" can last a
                   whole in-flight turn; the write leg lands and a refused fire
                   shows inline. */
                disabled={saving || writeDisabled || finished || !message.trim() || (loop.active && cycleAlreadyDue && !formDirty)}
                aria-label={playName}
                title={playName}
              >
                {loop.active ? <Zap size={14} aria-hidden /> : <Play size={14} aria-hidden />}
                {formDirty && (
                  <span data-testid="auto-nudge-play-dirty" aria-hidden className="absolute -top-0.5 -right-0.5 h-1.5 w-1.5 rounded-full bg-warn ring-1 ring-bg" />
                )}
              </Btn>
            </div>
          </div>
        ) : (
          <div className="flex items-center justify-end gap-2" data-testid="auto-nudge-actions">
            <div className="flex items-center gap-2" data-testid="auto-nudge-controls">
              <Btn
                type="button"
                primary
                className={ICON_BTN}
                onClick={startNow}
                disabled={saving || writeDisabled || !message.trim()}
                aria-label={playName}
                title={playName}
              >
                <Play size={14} aria-hidden />
              </Btn>
            </div>
          </div>
        )}
      </PopoverContent>}
    </Popover>
  )
}
