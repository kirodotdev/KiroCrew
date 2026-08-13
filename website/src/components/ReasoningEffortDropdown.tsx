import { useQuery } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { effortLabel } from './ChatInput'
import { EFFORT_LEVELS } from '../lib/effort'
import { api } from '../api/client'
import { Slider, Toggle } from './ui'
import InfoTip from './InfoTip'

import { i18nT } from '../i18n/t'

// Cold-start fallback before /api/effort-levels resolves (or on fetch failure).
// Concrete levels only — "default" is a separate toggle, not a slider notch.
const FALLBACK_LEVELS: string[] = EFFORT_LEVELS.filter(Boolean)

function normalizeLevels(data: string[]): string[] {
  return data.filter(l => l !== '' && l !== 'default')
}

interface Props {
  slot: string
  currentEffort: string
  /** Configured default effort for new sessions ('' = none). The slot's own
   *  value stays the source of truth for the toggle — this only labels what the
   *  no-override state actually inherits. */
  defaultEffort?: string
  /** Kept for call-site compatibility; the slider stays open while adjusting
   *  and the popover dismisses on outside-click, so this is no longer invoked. */
  onClose: () => void
  embedded?: boolean
}

/** Reasoning-effort picker: a stepped macOS-style slider over the model's
 *  ordered effort levels (Default → low → … → max). Each notch is a level;
 *  the value snaps to the grid and persists to the slot. Reads the slot's
 *  live levels from /api/effort-levels (keyed by slot so a model switch is
 *  reflected on remount). */
export default function ReasoningEffortDropdown({ slot, currentEffort, defaultEffort = '', embedded }: Props) {
  const { data: levels = FALLBACK_LEVELS } = useQuery({
    queryKey: ['effort-levels', slot],
    queryFn: () => api.effortLevels(slot).then(data =>
      Array.isArray(data) && data.length > 0
        ? normalizeLevels(data)
        : FALLBACK_LEVELS
    ),
    staleTime: 0,
    refetchOnMount: 'always',
  })

  // "Default" is a mode (let the model pick its own effort), not a level — it's a
  // toggle. The slider covers only the concrete levels (low→max).
  const propDefault = currentEffort === ''
  // Include the slot's current concrete level if the model didn't report it.
  // The "" default sentinel is never a notch (guarded by the truthy check).
  const concrete = currentEffort && !levels.includes(currentEffort) ? [...levels, currentEffort] : levels
  const maxIdx = Math.max(0, concrete.length - 1)
  const currentIdx = concrete.indexOf(currentEffort)

  // Optimistic default state so the toggle flips instantly; the persisted value
  // catches up after the debounced write + slot refresh.
  const [isDefault, setIsDefault] = useState(propDefault)
  useEffect(() => { setIsDefault(propDefault) }, [propDefault])

  // idx = the concrete level the slider points at. Persists while Default is on
  // so toggling Default off restores the user's last explicit pick.
  const [idx, setIdx] = useState(() => currentIdx >= 0 ? currentIdx : Math.min(2, maxIdx))
  useEffect(() => { if (currentIdx >= 0) setIdx(currentIdx) }, [currentIdx])

  const commitTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const pendingLevel = useRef<string | null>(null)
  useEffect(() => () => {
    if (commitTimer.current) {
      clearTimeout(commitTimer.current)
      // Flush a pending write so closing the dropdown within the 150ms debounce
      // window doesn't silently drop the user's last effort change.
      if (pendingLevel.current !== null) {
        api.chatSlotReasoningEffort(slot, pendingLevel.current).catch(() => {})
      }
    }
  }, [slot])

  // Persist debounced so a drag across several notches doesn't spam the backend.
  const commit = (level: string) => {
    pendingLevel.current = level
    if (commitTimer.current) clearTimeout(commitTimer.current)
    commitTimer.current = setTimeout(async () => {
      pendingLevel.current = null
      try { await api.chatSlotReasoningEffort(slot, level) }
      // eslint-disable-next-line no-console -- intentional failure diagnostic
      catch (err) { console.warn('Failed to set reasoning effort', err) }
    }, 150)
  }

  const handleSlide = (next: number) => { setIsDefault(false); setIdx(next); commit(concrete[next] ?? '') }
  const handleToggleDefault = (useDefault: boolean) => { setIsDefault(useDefault); commit(useDefault ? '' : (concrete[idx] ?? '')) }

  // In the no-override state, name what is actually inherited. A bare "Default"
  // implied the model picks its own effort, which is false once a default is
  // configured in Settings → Chat.
  const currentLabel = isDefault
    ? (defaultEffort
        ? i18nT('components.reasoningEffortDropdown.default_with_level', { level: effortLabel(defaultEffort) })
        : i18nT('lib.effort.default'))
    : effortLabel(concrete[idx] ?? '')
  const atMax = !isDefault && idx >= maxIdx
  // Turning the toggle on clears the per-slot override — which yields the
  // Settings default when one is configured, and only the model's own choice
  // when it is not. Label each case for what it actually does.
  const defaultToggleLabel = defaultEffort
    ? i18nT('components.reasoningEffortDropdown.use_configured_default')
    : i18nT('components.reasoningEffortDropdown.use_model_default')

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
        onChange={handleSlide}
        disabled={isDefault}
        emphasizeMax={!isDefault}
        formatValue={v => effortLabel(concrete[v] ?? '')}
      />
      <div className={`relative mt-1 h-[14px] text-[10px] text-muted select-none transition-opacity ${isDefault ? 'opacity-40' : ''}`}>
        <span className="absolute left-0">{i18nT('components.reasoningEffortDropdown.faster')}</span>
        <span className="absolute right-0">{i18nT('components.reasoningEffortDropdown.smarter')}</span>
      </div>
      <div className="flex items-center justify-between gap-2 mt-3.5">
        <span className="text-[12px] text-text">{defaultToggleLabel}</span>
        <Toggle checked={isDefault} onChange={handleToggleDefault} label={defaultToggleLabel} />
      </div>
    </div>
  )
}
