import { useQuery } from '@tanstack/react-query'
import { useCallback, useEffect, useRef, useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { effortLabel } from './ChatInput'
import { EFFORT_LEVELS, effortLevelsForModel, nearestSupportedEffort } from '../lib/effort'
import { api } from '../api/client'
import { pendingSlotSwitchTarget, performSlotSwitch, stageSlotSwitchTarget } from '../lib/slotSwitch'
import { useAppDispatch } from '../store'
import { updateSlot } from '../store/dashboardSlice'
import { setAgentSwitchNotice } from '../store/chatSlice'
import { agentSwitchFailureMessage } from '../utils/agentSwitchFeedback'
import { Slider } from './ui'
import InfoTip from './InfoTip'

import { i18nT } from '../i18n/t'

// Cold-start fallback before /api/effort-levels resolves (or on fetch failure).
// Concrete levels only — inherited default is a mode, not a slider notch.
const FALLBACK_LEVELS: string[] = EFFORT_LEVELS.filter(Boolean)

function normalizeLevels(data: string[]): string[] {
  return data.filter(l => l !== '' && l !== 'default')
}

interface Props {
  slot: string
  currentEffort: string
  /** The effort this session inherits when it carries no override, as the
   *  backend resolves it (crew pin → role default → global default). '' means
   *  no tier pins one and the model decides. The slot's own value stays the
   *  source of truth for whether an override exists. */
  defaultEffort?: string
  /** The model the slider describes ('' = auto/unknown). Decides which levels
   *  are offered and what the inherited default clamps to on this model. */
  model?: string
  /** Kept for call-site compatibility; the slider stays open while adjusting
   *  and the popover dismisses on outside-click, so this is no longer invoked. */
  onClose: () => void
  embedded?: boolean
  /** Effort levels to offer instead of this machine's.
   *
   *  Set for a session bound to a peer crew for execution: the levels come from
   *  the model running the turn, which is the PEER's model, and
   *  `/api/effort-levels` only knows about this gateway. An empty array is
   *  meaningful — "the peer's levels could not be read" — and falls back to the
   *  shared fallback set rather than to this machine's live values, because those
   *  would describe a model that is not answering. */
  levelsOverride?: string[]
}

/** Reasoning-effort picker: a stepped macOS-style slider over the model's
 *  ordered effort levels (Default → low → … → max). Each notch is a level;
 *  the value snaps to the grid and persists to the slot. Reads the slot's
 *  live levels from /api/effort-levels (keyed by slot so a model switch is
 *  reflected on remount). */
export default function ReasoningEffortDropdown({ slot, currentEffort, defaultEffort = '', model = '', embedded, levelsOverride }: Props) {
  // Per-model table first: kiro-cli never reports levels over the protocol, so
  // the live query below only ever answers with a process-wide list that may
  // describe whichever model last synced. The table is what keeps the notches
  // matched to the model the composer shows.
  const staticLevels = effortLevelsForModel(model) || undefined
  const { data: liveLevels = FALLBACK_LEVELS } = useQuery({
    queryKey: ['effort-levels', slot],
    queryFn: () => api.effortLevels(slot).then(data =>
      Array.isArray(data) && data.length > 0
        ? normalizeLevels(data)
        : FALLBACK_LEVELS
    ),
    staleTime: 0,
    refetchOnMount: 'always',
    // A peer-bound session never consults this gateway's levels, so it must not
    // spawn the query either — the answer would describe the wrong model. A
    // model the table knows does not need it either.
    enabled: levelsOverride === undefined && staticLevels === undefined,
  })
  const levels: readonly string[] = levelsOverride !== undefined
    ? (levelsOverride.length > 0 ? normalizeLevels(levelsOverride) : FALLBACK_LEVELS)
    : (staticLevels ?? liveLevels)

  // What this session RUNS at: its own override when it has one, else the level
  // the crew / Settings chain resolves to — either clamped to what this model
  // accepts (an xhigh pick or pin runs high on Sonnet 4.6). The slider shows
  // that one level; there is no separate "default" state to read or reset.
  // Only a chain that resolves to nothing has no notch: the model decides and
  // its choice is not reported, so no thumb is drawn until the user picks.
  const concrete = levels
  const maxIdx = Math.max(0, concrete.length - 1)
  const hasOverride = currentEffort !== ''
  const requested = hasOverride ? currentEffort : defaultEffort
  const shown = nearestSupportedEffort(requested, concrete)
  const shownIdx = concrete.indexOf(shown)
  const unresolved = shownIdx < 0

  // The notch a pick moved the thumb to, held until the persisted value catches
  // up (150ms debounce + slot refresh) or the write fails. `null` = show props.
  const [optimisticIdx, setOptimisticIdx] = useState<number | null>(null)
  useEffect(() => { setOptimisticIdx(null) }, [shownIdx, slot])
  const idx = optimisticIdx ?? (shownIdx >= 0 ? shownIdx : Math.min(2, maxIdx))
  const hidePosition = optimisticIdx === null && unresolved

  // Persist one level pick through the shared switch protocol (#4523): the
  // local optimistic state above masks staleness in THIS popover, but the
  // STORE is the base the Alt+Shift effort cycle steps from — without the
  // write, a dropdown pick followed by a cycle press steps from the
  // pre-pick value. performSlotSwitch serializes per slot+field and writes
  // exactly the adjudicated survivor of a burst of picks.
  const dispatch = useAppDispatch()
  const persistEffort = useCallback((level: string) =>
    performSlotSwitch('reasoning_effort', slot, level,
      async () => {
        const r = await api.chatSlotReasoningEffort(slot, level)
        return r?.reasoning_effort ?? level
      },
      (value) => dispatch(updateSlot({ key: slot, reasoning_effort: value }))),
  [slot, dispatch])

  const announcePersistFailure = useCallback((error: unknown, failedLevel: string) => {
    // A superseded request may still reject after a newer pick was staged or
    // began. That older failure changed no current intent, so it must not flash
    // a misleading notice for the newer selection. A confirmation timeout,
    // however, leaves its own wire request pending; identity distinguishes that
    // unconfirmed current pick from a genuinely newer target.
    const pending = pendingSlotSwitchTarget('reasoning_effort', slot)
    if (pending !== null && pending !== failedLevel) return false
    dispatch(setAgentSwitchNotice(agentSwitchFailureMessage(error)))
    return true
  }, [dispatch, slot])

  const commitTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const pendingLevel = useRef<string | null>(null)
  useEffect(() => () => {
    if (commitTimer.current) {
      clearTimeout(commitTimer.current)
      // Flush a pending write so closing the dropdown within the 150ms debounce
      // window doesn't silently drop the user's last effort change — but only
      // while the pick is still the newest intent (same staleness gate as the
      // timer below).
      const level = pendingLevel.current
      if (level !== null && pendingSlotSwitchTarget('reasoning_effort', slot) === level) {
        persistEffort(level).catch((err: unknown) => { announcePersistFailure(err, level) })
      }
    }
  }, [announcePersistFailure, persistEffort, slot])

  // Persist debounced so a drag across several notches doesn't spam the backend.
  // The pick is STAGED synchronously so the Alt+Shift effort-cycle shortcuts
  // see it as the newest intent inside the debounce window — without this, a
  // dropdown pick followed by a cycle press within 150ms steps from the
  // pre-pick base and re-selects the pick instead of advancing past it.
  const commit = (level: string) => {
    stageSlotSwitchTarget('reasoning_effort', slot, level)
    pendingLevel.current = level
    if (commitTimer.current) clearTimeout(commitTimer.current)
    commitTimer.current = setTimeout(async () => {
      pendingLevel.current = null
      // A cycle shortcut may have superseded this pick inside the debounce
      // window (its request begins immediately and clears the stage). Firing
      // the stale pick now would make it the NEWEST request and win the
      // adjudication — reverting the user's newer choice. Persist only while
      // this pick is still the newest declared intent.
      if (pendingSlotSwitchTarget('reasoning_effort', slot) !== level) return
      try { await persistEffort(level) }
      catch (err) {
        // Back to the authoritative props, not the notch the failed pick chose.
        if (announcePersistFailure(err, level)) setOptimisticIdx(null)
        // eslint-disable-next-line no-console -- visible notice above; retain diagnostic detail
        console.warn('Failed to set reasoning effort', err)
      }
    }, 150)
  }

  // Every landing is this session's pick: a drag to another notch, a click on
  // the notch already shown (an inherited level the user now owns outright),
  // or Enter. Picking the level the session already holds explicitly is a
  // no-op — nothing would change on the wire.
  const handlePick = (next: number) => {
    const level = concrete[next] ?? ''
    if (hasOverride && level === currentEffort) return
    setOptimisticIdx(next)
    commit(level)
  }

  // Header names the level in force. When the chain ends at the model's own
  // unreported default, say so but keep the axis directly interactive.
  const currentLabel = hidePosition
    ? i18nT('components.reasoningEffortDropdown.model_default')
    : effortLabel(concrete[idx] ?? '')
  const atMax = !hidePosition && idx >= maxIdx

  return (
    <div className={embedded ? 'px-3 py-2.5' : 'rounded-lg bg-bg-elevated border border-border px-4 py-3.5 w-[240px]'}>
      <div className="flex items-center gap-1.5 mb-3">
        <span className="text-[14px] font-medium text-muted uppercase tracking-[.04em] leading-none">{i18nT('components.reasoningEffortDropdown.effort')}</span>
        <span className="relative inline-flex items-center overflow-hidden leading-none" style={{ height: '1.5em' }}>
          <AnimatePresence mode="popLayout" initial={false}>
            <motion.span
              key={currentLabel}
              initial={{ y: '100%' }}
              animate={{ y: 0 }}
              exit={{ y: '-100%' }}
              transition={{ type: 'spring', stiffness: 500, damping: 34 }}
              className={`inline-flex items-center h-full text-[14px] font-semibold whitespace-nowrap leading-none transition-colors ${atMax ? 'text-accent' : 'text-text'}`}
            >
              {currentLabel}
            </motion.span>
          </AnimatePresence>
        </span>
        <span className="ml-auto flex"><InfoTip text={i18nT('components.reasoningEffortDropdown.effort_help')} placement="top" /></span>
      </div>
      <Slider
        aria-label={i18nT('components.reasoningEffortDropdown.reasoning_effort')}
        min={0}
        max={maxIdx}
        step={1}
        value={idx}
        onPick={handlePick}
        emphasizeMax={!hidePosition}
        hidePosition={hidePosition}
        unresolvedValueText={i18nT('components.reasoningEffortDropdown.model_default')}
        formatValue={v => effortLabel(concrete[v] ?? '')}
      />
      <div className="relative mt-1 h-[14px] select-none text-[10px] text-muted">
        <span className="absolute left-0">{i18nT('components.reasoningEffortDropdown.faster')}</span>
        <span className="absolute right-0">{i18nT('components.reasoningEffortDropdown.smarter')}</span>
      </div>
    </div>
  )
}
