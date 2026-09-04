import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Goal, X } from 'lucide-react'
import { Popover, PopoverTrigger, PopoverContent } from './ui/popover'
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
  /**
   * True when the slot's last turn ended interrupted (the composer is showing
   * Resume). The chip stops pulsing and turns warn-coloured: the loop is still
   * armed, but nothing is running until the user resumes or the next idle-timer
   * cycle fires, and a pulsing chip would claim active work for that whole gap.
   */
  interrupted?: boolean
}

const DEFAULT_MSG = `Your north star is in north_star.md, roadmap in roadmap.md, tasks in tasks.md. Pick the single highest-leverage next step toward the goal and execute it. Update tasks.md. Post a blocker ONCE if genuinely stuck. To halt the loop, create {{STOP_FILE}}`

/** One armed script cron owned by this chat slot. */
interface SlotWatch {
  id: string
  name: string
  schedule: string
  next_run_ts: number | null
}

export default function AutoNudgePopover({ slotKey, loop, open, onOpenChange, onChange, interrupted = false }: Props) {
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
    enabled: open,
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

  // Seed/restore fields on each open (rising edge). A live loop is the
  // authoritative source; otherwise the last per-slot draft is restored.
  // One read seeds all three fields. Runs in an effect (not render) so the
  // render itself performs no storage read/write.
  useEffect(() => {
    if (!open) return
    hasEdited.current = false
    setError('')
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

  async function save() {
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
      const resp = await fetch(`/api/autonudge/${loop.id}`, { method: 'DELETE' })
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
      <PopoverContent
        side="top"
        align="start"
        /* Viewport-capped rather than a pinned 420px: at the 320px floor a fixed
           width pushes this panel -- and the right-aligned action below -- past the
           usable viewport. Written as a max so there is no `md:` counterpart to keep
           in sync: 420px is simply the ceiling, and a phone gets the width it has. */
        className="w-[min(420px,calc(100vw_-_1.5rem))] p-4 text-[12px]"
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
        <textarea
          aria-label={i18nT('components.autoNudgePopover.goal_description')}
          value={message}
          onChange={e => { hasEdited.current = true; setMessage(e.target.value) }}
          rows={6}
          className="w-full bg-bg border border-border rounded p-2 text-[12px] font-mono resize-y mb-3 text-text"
          placeholder={i18nT('components.autoNudgePopover.describe_what_you_want_the_agent_to_accomplish')}
        />

        <div className="flex gap-3 mb-3">
          <div className="flex-1">
            <div className="text-muted text-[11px] mb-1">{i18nT('components.autoNudgePopover.seconds_between_nudges')}</div>
            <input
              type="number"
              aria-label={i18nT('components.autoNudgePopover.seconds_between_nudges')}
              min={15}
              max={86400}
              value={idleInput}
              onChange={e => { hasEdited.current = true; setIdleInput(e.target.value) }}
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
              onChange={e => { hasEdited.current = true; setMaxCyclesInput(e.target.value) }}
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
                 blind reader of the paused screenshot could not tell it was the
                 same loop at all, and an inactive loop otherwise looks identical
                 to an active one whose button failed to render -- the state is
                 the reason for the absence, so it belongs in the space the
                 absence leaves. Text, not a disabled button: the server refuses
                 to fire an inactive loop, so there is no press to offer. */
              <span
                data-testid="auto-nudge-loop-paused"
                className="text-muted text-[11px] shrink-0"
              >
                {i18nT('components.autoNudgePopover.loop_paused')}
              </span>
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
            <button
              onClick={stop}
              disabled={saving}
              className="px-3 py-1 rounded border border-border text-muted hover:text-danger hover:border-danger bg-transparent cursor-pointer disabled:opacity-50"
            >
              {i18nT('components.autoNudgePopover.stop_loop')}
            </button>
          )}
          <button
            onClick={save}
            disabled={saving || !message.trim()}
            className="px-3 py-1 rounded bg-accent text-accent-fg border-none cursor-pointer disabled:opacity-50 hover:bg-accent/90"
          >
            {/* A paused loop's way out was invisible: this button silently PATCHes
                `active: true`, so on an inactive loop it must SAY so. A usability
                reader found no resume control at all and called both "Paused" and
                "Stop loop" risky as a result. Gated on `active`, not on existence,
                which is the bug -- and it reuses the `start_loop` key the no-loop
                case already uses, so no catalogue gains a string. */}
            {loop?.active
              ? i18nT('components.autoNudgePopover.save')
              : i18nT('components.autoNudgePopover.start_loop')}
          </button>
        </div>
      </PopoverContent>
    </Popover>
  )
}
