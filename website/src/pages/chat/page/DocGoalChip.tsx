/**
 * A doc heading's goal control (PoC): after the heading's text, a "Watching" chip
 * once its section has a goal, or a quiet "Set goal" that shows on hover. Either
 * opens the goal popover: what to keep checking inside the section, and how often,
 * the same choices a board section offers. Labels are English like the rest of the
 * doc conductor layer.
 */
import { useEffect, useRef, useState } from 'react'
import { motion, useReducedMotion } from 'framer-motion'
import { Target } from 'lucide-react'
import { Btn } from '../../../components/ui'
import { timeAgo } from '../../../utils/timeAgo'
import { GOAL_INTERVALS } from './board/boardModel'
import type { DocGoal, GoalCheck } from './docGoals'

const minutes = (m: number) => (m < 60 ? `${m} min` : '1 hr')

/** How often "2m ago" is read again; a minute is the finest it shows. */
const AGO_REFRESH_MS = 30_000
const BAR_ACTED = 12
const BAR_QUIET = 4

/** The status dot, breathing while the section is watched; still under reduced motion. */
function WatchDot() {
  const reduce = useReducedMotion()
  return (
    <motion.span data-gc="dot" aria-hidden
      animate={reduce ? undefined : { boxShadow: ['0 0 0 0 color-mix(in srgb, var(--accent) 45%, transparent)', '0 0 0 6px color-mix(in srgb, var(--accent) 0%, transparent)'] }}
      transition={reduce ? undefined : { duration: 2.4, ease: 'easeOut', repeat: Infinity }} />
  )
}

export default function DocGoalChip({ goal, title, every, checks = [], onGoal, onEvery }: {
  goal: DocGoal | null
  title: string
  /** The page's one rate, shared by every goal on it. Each check's report is on
   *  the heading's pin, not here. */
  every: number
  /** This goal's recent finished checks, oldest first: drawn as a row of bars. */
  checks?: GoalCheck[]
  onGoal: (g: DocGoal | undefined) => void
  onEvery: (minutes: number) => void
}) {
  const [open, setOpen] = useState(false)
  const [text, setText] = useState('')
  const root = useRef<HTMLSpanElement>(null)
  const field = useRef<HTMLTextAreaElement>(null)
  // Re-reads the clock so "2m ago" keeps up while the page sits open.
  const [, setNow] = useState(0)
  useEffect(() => {
    const timer = setInterval(() => setNow(n => n + 1), AGO_REFRESH_MS)
    return () => clearInterval(timer)
  }, [])
  const lastTs = checks.at(-1)?.ts
  const ago = lastTs ? timeAgo(lastTs / 1000) : ''
  const acted = checks.filter(c => c.acted).length

  useEffect(() => {
    if (!open) return
    setText(goal?.text ?? '')
    // Focused without scrolling the page under the pointer.
    requestAnimationFrame(() => field.current?.focus({ preventScroll: true }))
    const away = (e: PointerEvent) => { if (!root.current?.contains(e.target as Node)) setOpen(false) }
    // Listening starts once the press that opened it is over.
    const start = setTimeout(() => document.addEventListener('pointerdown', away), 0)
    return () => { clearTimeout(start); document.removeEventListener('pointerdown', away) }
    // Reset the draft only when the popover opens.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  const save = () => {
    const t = text.trim()
    if (!t) return
    onGoal({ text: t })
    setOpen(false)
  }

  return (
    <span ref={root} data-gc="root">
      {goal ? (
        <button type="button" data-gc="chip" data-testid="doc-goal-chip" aria-expanded={open} title={goal.text}
          aria-label={`Watching: ${goal.text}.${checks.length ? ` ${acted} of the last ${checks.length} checks found something.${ago ? ` Checked ${ago}.` : ''}` : ''} Edit the goal`}
          onMouseDown={e => e.preventDefault()} onClick={() => setOpen(o => !o)}>
          <WatchDot /> Watching
          {checks.length > 0 && (
            <span data-gc="beat" data-testid="doc-goal-beat" aria-hidden>
              {checks.map((c, i) => <i key={i} data-acted={c.acted ? 'true' : 'false'} style={{ height: c.acted ? BAR_ACTED : BAR_QUIET }} />)}
            </span>
          )}
          {ago && <span data-gc="ago" data-testid="doc-goal-ago">{ago}</span>}
        </button>
      ) : (
        <button type="button" data-gc="add" data-goal="add" data-testid="doc-goal-add" aria-expanded={open} aria-label={`Set a goal for ${title}`}
          onMouseDown={e => e.preventDefault()} onClick={() => setOpen(o => !o)}>
          <Target size={13} aria-hidden /> Set goal
        </button>
      )}
      {open && (
        <span role="dialog" aria-label="Section goal" data-gc="pop" data-testid="doc-goal-popover">
          <span data-gc="pop-title">Goal for “{title}”</span>
          <span data-gc="pop-help">A watcher keeps checking what is in this section, down to the next heading at this level, and nothing outside it.</span>
          <textarea ref={field} aria-label="Goal" data-testid="doc-goal-text" rows={3} value={text}
            placeholder="Keep checking these tickets and mark the ones that are done"
            onChange={e => setText(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Escape') { e.preventDefault(); setOpen(false) }
              if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); save() }
            }} />
          {/* One rate for the page: every goal here is checked on the same wake. */}
          <span role="radiogroup" aria-label="Check every goal on this page every" data-gc="every-row">
            <span data-gc="every-label">Check all goals every</span>
            {GOAL_INTERVALS.map(m => (
              <button key={m} type="button" role="radio" aria-checked={every === m} data-gc="every-opt" onClick={() => onEvery(m)}>{minutes(m)}</button>
            ))}
          </span>

          <span data-gc="actions">
            {goal ? <Btn danger data-testid="doc-goal-clear" onClick={() => { onGoal(undefined); setOpen(false) }}>Stop watching</Btn> : <span />}
            <Btn primary data-testid="doc-goal-save" disabled={!text.trim()} onClick={save}>{goal ? 'Update' : 'Start watching'}</Btn>
          </span>
        </span>
      )}
    </span>
  )
}
