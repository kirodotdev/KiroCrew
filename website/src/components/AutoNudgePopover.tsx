import { useEffect, useRef, useState } from 'react'
import { Goal, X } from 'lucide-react'
import { Popover, PopoverTrigger, PopoverContent } from './ui/popover'
import { loadGoalDraft, saveGoalDraft, type GoalDraft } from '../utils/goalDrafts'
import { DRAFT_SAVE_DEBOUNCE_MS } from '../utils/draftConstants'

import { i18nT } from '../i18n/t'
import { fmtTimeNumeric } from '../i18n/format'
export interface AutoNudgeLoop {
  id: string
  slot_key: string
  message: string
  idle_secs: number
  max_cycles: number
  cycle_count: number
  active: boolean
  last_fire_ts: number
}

interface Props {
  slotKey: string
  loop: AutoNudgeLoop | null
  open: boolean
  onOpenChange: (open: boolean) => void
  onChange: (loop: AutoNudgeLoop | null) => void
}

const DEFAULT_MSG = `Your north star is in north_star.md, roadmap in roadmap.md, tasks in tasks.md. Pick the single highest-leverage next step toward the goal and execute it. Update tasks.md. Post a blocker ONCE if genuinely stuck. To halt the loop, create {{STOP_FILE}}`

export default function AutoNudgePopover({ slotKey, loop, open, onOpenChange, onChange }: Props) {
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

  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      <PopoverTrigger asChild>
        <button
          className={`h-8 px-2 rounded-lg text-[12px] font-mono flex items-center gap-1 cursor-pointer transition-all bg-transparent border-none shrink-0 whitespace-nowrap ${
            loop?.active
              ? 'text-accent hover:text-accent hover:bg-accent/10 animate-pulse'
              : 'text-muted hover:text-text hover:bg-bg-hover'
          }`}
          title={loop?.active ? i18nT('components.autoNudgePopover.goal_active_cycle', { cycle: loop.cycle_count }) : i18nT('components.autoNudgePopover.set_a_goal')}
          aria-label={loop?.active ? i18nT('components.autoNudgePopover.goal_active_cycle', { cycle: loop.cycle_count }) : i18nT('components.autoNudgePopover.set_a_goal')}
        >
          <Goal size={16} className="shrink-0" />
          {loop?.active && loop.cycle_count > 0 ? loop.cycle_count : null}
        </button>
      </PopoverTrigger>
      <PopoverContent side="top" align="start" className="w-[420px] p-4 text-[12px]">
        <div className="flex items-center justify-between mb-2">
          <div className="flex items-center gap-2 font-medium text-text">
            <Goal size={14} className={loop?.active ? 'text-accent' : 'text-muted'} />
            {i18nT('components.autoNudgePopover.set_a_goal')}
            {loop?.active && <span className="text-muted text-[11px]">{i18nT('components.autoNudgePopover.cycle')} {loop.cycle_count}</span>}
          </div>
          <button aria-label={i18nT('components.autoNudgePopover.close')} onClick={() => onOpenChange(false)} className="text-muted hover:text-text bg-transparent border-none cursor-pointer">
            <X size={14} />
          </button>
        </div>
        <p className="text-muted text-[11px] mb-3 leading-relaxed">{i18nT('components.autoNudgePopover.give_the_agent_a_goal_and_it_will_keep_working_t')}</p>

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
            <div className="text-muted text-[11px] mb-1">{i18nT('components.autoNudgePopover.idle_seconds_before_nudge')}</div>
            <input
              type="number"
              aria-label={i18nT('components.autoNudgePopover.idle_seconds_before_nudge')}
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

        {loop && (
          <div className="text-muted text-[11px] mb-3">
            {i18nT('components.autoNudgePopover.last_fire')} {loop.last_fire_ts ? fmtTimeNumeric(loop.last_fire_ts) : i18nT('components.autoNudgePopover.never')}
          </div>
        )}

        {error && <div className="text-danger text-[11px] mb-2">{error}</div>}

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
            {loop ? i18nT('components.autoNudgePopover.save') : i18nT('components.autoNudgePopover.start_loop')}
          </button>
        </div>
      </PopoverContent>
    </Popover>
  )
}
