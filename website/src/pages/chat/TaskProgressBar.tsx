import { useMemo, useCallback, memo } from 'react'
import { ListTodo, ChevronDown, ChevronRight, CheckCircle2, Circle } from 'lucide-react'
import { useAppSelector } from '../../store'
import { sanitizeLlmOutput } from '../../utils/sanitize'
import type { TodoList } from '../../types'
import { useRowDisclosure } from './rowDisclosure'

import { i18nT } from '../../i18n/t'
/** Rows rendered before the list scrolls internally — bounds DOM on long plans. */
const MAX_VISIBLE_ROWS = 12

/**
 * The agent's TODO list as a collapsed pill above the chat composer.
 *
 * Reads `slot.todo` off the shared slots array, which is populated by BOTH the
 * `slots` snapshot (cold load / reconnect) and the live `todo_update` delta — so
 * the pill survives a refresh mid-turn without its own rehydration path.
 *
 * Renders nothing when the agent has never used its todo tool. An empty-but-
 * present list is also hidden (there is nothing to show), but is distinct from
 * absent at the data layer.
 */
const TaskProgressBar = memo(function TaskProgressBar({ slot, disclosureKey }: { slot: string | null; disclosureKey?: string }) {
  const [expanded, setExpanded] = useRowDisclosure(disclosureKey, false)
  // Select the primitive-bearing todo object for this slot only, so unrelated
  // slot churn in the slots array doesn't re-render the pill.
  const todo = useAppSelector(s =>
    (s.dashboard.slots ?? []).find(x => x.key === slot)?.todo ?? null
  ) as TodoList | null

  const tasks = useMemo(() => todo?.tasks ?? [], [todo])
  const toggle = useCallback(() => setExpanded(v => !v), [setExpanded])

  if (!slot || !todo || tasks.length === 0) return null

  const total = typeof todo.total === 'number' ? todo.total : tasks.length
  const done = typeof todo.completed === 'number' ? todo.completed : 0
  const allDone = total > 0 && done >= total
  // `current` is the first not-completed task (server-derived). When everything
  // is done there is no current task, so the label reports completion instead.
  const current = sanitizeLlmOutput(todo.current || '')
  const label = allDone ? i18nT('pages.chat.taskProgressBar.all_tasks_complete') : current || i18nT('pages.chat.taskProgressBar.current_task')
  const pct = total > 0 ? Math.round((done / total) * 100) : 0

  return (
    <div className="px-5 mx-auto w-full" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
      {/* Collapsed = a small pill that hugs its content; expanded = a full-width
          panel. Keeping the collapsed state inline stops it reading as another
          full-width bar competing with the composer below it. */}
      <div
        className={`mb-1 animate-slide-up overflow-hidden border border-accent/20 bg-accent/10 ${
          expanded ? 'rounded-md' : 'rounded-full inline-flex max-w-full'
        }`}
      >
        <button
          type="button"
          data-testid="todo-pill"
          onClick={toggle}
          aria-expanded={expanded}
          aria-label={`${done} of ${total} tasks complete. ${expanded ? 'Collapse' : 'Expand'} task list`}
          className={`flex items-center gap-2 py-1.5 text-[13px] font-mono bg-transparent border-none cursor-pointer hover:bg-accent/5 transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent ${
            expanded ? 'w-full px-3' : 'px-3 min-w-0'
          }`}
        >
          {expanded
            ? <ChevronDown size={14} className="text-accent shrink-0" aria-hidden="true" />
            : <ChevronRight size={14} className="text-accent shrink-0" aria-hidden="true" />}
          <ListTodo size={14} className="text-accent shrink-0" aria-hidden="true" />
          <span
            className={`shrink-0 tabular-nums font-medium ${allDone ? 'text-ok' : 'text-text-strong'}`}
            data-testid="todo-count"
          >
            {done} {i18nT('pages.chat.taskProgressBar.of')} {total}
          </span>
          <span
            className={`truncate text-left text-muted ${expanded ? 'min-w-0 flex-1' : 'min-w-0 max-w-[42ch]'}`}
            data-testid="todo-current"
          >
            {label}
          </span>
          {/* Thin progress rail — a glanceable second channel for the same count. */}
          <span
            className={`shrink-0 h-1 w-12 rounded-full bg-border/60 overflow-hidden ${expanded ? '' : 'ml-1'}`}
            role="progressbar"
            aria-valuenow={done}
            aria-valuemin={0}
            aria-valuemax={total}
            aria-label={i18nT('pages.chat.taskProgressBar.task_completion')}
          >
            <span
              className={`block h-full rounded-full transition-all ${allDone ? 'bg-ok' : 'bg-accent'}`}
              style={{ width: `${pct}%` }}
            />
          </span>
        </button>
        {expanded && (
          <ul
            data-testid="todo-list"
            className="px-3 pb-2 space-y-0.5 list-none m-0 max-h-64 overflow-y-auto"
          >
            {todo.description && (
              <li className="text-[11px] text-muted/70 font-mono pb-1 truncate">
                {sanitizeLlmOutput(todo.description)}
              </li>
            )}
            {tasks.slice(0, MAX_VISIBLE_ROWS).map((t, i) => (
              <li
                key={t.id || i}
                data-testid="todo-row"
                className="flex items-start gap-1.5 text-[12px] font-mono"
              >
                {t.completed
                  ? <CheckCircle2 size={12} className="mt-[3px] shrink-0 text-ok" aria-hidden="true" />
                  : <Circle size={12} className="mt-[3px] shrink-0 text-muted/50" aria-hidden="true" />}
                <span className={t.completed ? 'text-muted/60 line-through' : 'text-text'}>
                  {sanitizeLlmOutput(t.text || '')}
                </span>
              </li>
            ))}
            {tasks.length > MAX_VISIBLE_ROWS && (
              <li className="text-[11px] text-muted/60 font-mono pl-[18px]">
                + {tasks.length - MAX_VISIBLE_ROWS} {i18nT('pages.chat.taskProgressBar.more')}
              </li>
            )}
          </ul>
        )}
      </div>
    </div>
  )
})

export default TaskProgressBar
