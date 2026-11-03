/**
 * The goal button of a section's toolbar and the popover behind it: what to keep
 * checking for the shapes inside the section, and how often. Labels are hardcoded
 * English like the rest of the board canvas.
 */

import { Target } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

import { DEFAULT_GOAL_INTERVAL, GOAL_INTERVALS, type SectionGoal } from './boardModel'
import { PANEL } from './SwatchPicker'

const RING = '#9747FF'

export default function GoalPopover({ goal, onGoal, every = DEFAULT_GOAL_INTERVAL, onEvery, latest = null }: {
  goal?: SectionGoal
  onGoal: (g: SectionGoal | undefined) => void
  /** The board's one rate, shared by every section goal on it. */
  every?: number
  onEvery?: (minutes: number) => void
  /** What the latest goal check said. */
  latest?: string | null
}) {
  const [open, setOpen] = useState(false)
  const [text, setText] = useState(goal?.text ?? '')
  const root = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    setText(goal?.text ?? '')
    const away = (e: PointerEvent) => { if (!root.current?.contains(e.target as Node)) setOpen(false) }
    document.addEventListener('pointerdown', away)
    return () => document.removeEventListener('pointerdown', away)
    // Reset the draft only when the popover opens.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])
  const save = () => {
    const t = text.trim()
    if (!t) return
    onGoal({ text: t, every, ...(goal?.session ? { session: goal.session } : {}) })
    setOpen(false)
  }
  return (
    <div ref={root} className="relative">
      <button type="button" data-testid="section-goal-button" aria-label="Section goal" title="Set a goal for this section" aria-expanded={open}
        onClick={() => setOpen(o => !o)} className="flex items-center gap-1.5 rounded-lg px-2 py-1 text-xs font-medium hover:bg-white/10"
        style={{ color: '#FFFFFF', background: goal ? 'rgba(151,71,255,.45)' : undefined }}>
        <Target size={14} /> Goal
      </button>
      {open && (
        <div role="dialog" aria-label="Section goal" data-testid="section-goal-popover" className="absolute left-1/2 flex w-[300px] flex-col gap-2 rounded-xl p-3 shadow-xl"
          style={{ bottom: '100%', marginBottom: 10, transform: 'translateX(-50%)', background: PANEL, color: '#FFFFFF', zIndex: 10 }}>
          <div className="text-[13px] font-semibold">Goal for this section</div>
          <div className="text-[12px]" style={{ opacity: 0.7 }}>A watcher keeps checking what is inside this section, and nothing outside it.</div>
          <textarea
            autoFocus
            aria-label="Goal"
            data-testid="section-goal-text"
            value={text}
            placeholder="Keep checking these tasks and mark the ones that are done"
            rows={3}
            onChange={e => setText(e.target.value)}
            onKeyDown={e => {
              e.stopPropagation()
              if (e.key === 'Escape') setOpen(false)
              if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) save()
            }}
            className="resize-none rounded-lg border-0 p-2 text-[13px] outline-none"
            style={{ background: 'rgba(255,255,255,.1)', color: '#FFFFFF' }}
          />
          {/* One rate for the board: every section goal is checked on the same wake. */}
          <div role="radiogroup" aria-label="Check every goal on this board every" className="flex items-center gap-1 text-[12px]">
            <span style={{ opacity: 0.7 }}>Check all goals every</span>
            {GOAL_INTERVALS.map(m => (
              <button key={m} type="button" role="radio" aria-checked={every === m} onClick={() => onEvery?.(m)}
                className="rounded-lg px-2 py-1" style={{ color: '#FFFFFF', background: every === m ? RING : 'rgba(255,255,255,.1)' }}>
                {m < 60 ? `${m} min` : '1 hr'}
              </button>
            ))}
          </div>
          {goal && (
            <div data-testid="section-goal-latest" className="flex max-h-[120px] flex-col gap-0.5 overflow-y-auto whitespace-pre-wrap rounded-lg p-2 text-[12px]"
              style={{ background: 'rgba(255,255,255,.08)', color: '#FFFFFF' }}>
              <span className="text-[11px]" style={{ opacity: 0.6 }}>Latest check</span>
              {latest ?? 'Not checked yet.'}
            </div>
          )}
          <div className="flex items-center justify-between gap-2">
            {goal ? (
              <button type="button" data-testid="section-goal-clear" onClick={() => { onGoal(undefined); setOpen(false) }}
                className="rounded-lg px-2 py-1 text-[12px] hover:bg-white/10" style={{ color: '#FFFFFF' }}>Stop watching</button>
            ) : <span />}
            <button type="button" data-testid="section-goal-save" disabled={!text.trim()} onClick={save}
              className="rounded-lg px-3 py-1 text-[12px] font-medium disabled:opacity-40" style={{ color: '#FFFFFF', background: RING }}>
              {goal ? 'Update' : 'Start watching'}
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
