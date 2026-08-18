/** A single task card on the kanban board. */
import { useSortable } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import {
  AlertCircle, Clock, GripVertical, Loader2, MessageSquare, Play,
} from 'lucide-react'
import { fmtRelative } from '../../../i18n/format'
import { i18nT } from '../../../i18n/t'
import type { TaskRecord } from '../types'

interface CardProps {
  task: TaskRecord
  onClick: (task: TaskRecord) => void
  onRun?: (task: TaskRecord) => void
  onOpenSession?: (sessionKey: string) => void
}

// Intl.RelativeTimeFormat picks the unit and renders the locale's own idiom,
// so "now" / "3m ago" is never assembled from an English suffix here.
function relativeTime(ts: number): string {
  return fmtRelative(ts * 1000)
}

export function TaskCard({ task, onClick, onRun, onOpenSession }: CardProps) {
  const {
    attributes, listeners, setNodeRef, transform, transition, isDragging,
  } = useSortable({ id: task.id, data: { task } })

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.5 : 1,
  }

  // The most recent run — what the card should surface.
  const latest = task.executions.length ? task.executions[task.executions.length - 1] : null
  const hasSession = Boolean(latest?.session_key)
  const failed = task.status === 'failed'
  const running = task.status === 'running'

  // A short, useful second line: the prompt when there's no description.
  const subtitle = task.description || task.prompt

  return (
    <div
      ref={setNodeRef}
      style={style}
      className={`
        group relative rounded-md border bg-card p-3 pl-4
        hover:border-border-strong hover:shadow-sm cursor-pointer
        transition-all duration-150
        ${failed ? 'border-danger/40' : running ? 'border-warn/40' : 'border-border'}
        ${isDragging ? 'shadow-lg ring-2 ring-accent/30' : ''}
      `}
      onClick={() => onClick(task)}
      role="button"
      tabIndex={0}
      onKeyDown={e => {
        // role="button" implies both keys activate; Space also scrolls by default.
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onClick(task)
        }
      }}
    >
      {/* Status accent stripe down the left edge */}
      <span
        aria-hidden
        className={`absolute left-0 top-0 bottom-0 w-[3px] rounded-l-md ${
          failed ? 'bg-danger' : running ? 'bg-warn' : task.status === 'done' ? 'bg-ok' : 'bg-border-strong'
        }`}
      />

      {/* Drag handle. dnd-kit's default sensors include the KeyboardSensor, so the
          handle is already operable with Space + arrows once focused -- but a
          hover-only reveal never shows it on touch and hides it while it holds
          focus, so the reveal follows focus and card-hover alike. */}
      <div
        {...attributes}
        {...listeners}
        className="absolute left-1.5 top-1/2 -translate-y-1/2 opacity-0 group-hover:opacity-60 focus-visible:opacity-100 group-focus-within:opacity-60 cursor-grab"
        onClick={e => e.stopPropagation()}
        aria-label={i18nT('apps.kanban.taskCard.drag_to_move')}
      >
        <GripVertical size={13} className="text-muted" />
      </div>

      <div className="pl-2">
        {/* Title + priority */}
        <div className="flex items-start gap-2">
          <h4 className="text-sm font-medium text-text-strong leading-snug flex-1 line-clamp-2">
            {task.title}
          </h4>
          {/* The card exists before it has a real name: say the title is still
              coming rather than presenting the provisional one as final. */}
          {task.refining && (
            <span
              className="shrink-0 flex items-center gap-1 text-[10px] text-muted"
              title={i18nT('apps.kanban.createTaskForm.refining')}
            >
              <Loader2 size={10} className="animate-spin" aria-hidden="true" />
              <span className="sr-only">{i18nT('apps.kanban.createTaskForm.refining')}</span>
            </span>
          )}
          {task.priority === 'high' && (
            <span className="shrink-0 text-[10px] font-semibold uppercase text-danger bg-danger-subtle px-1.5 py-0.5 rounded">
              {i18nT('apps.kanban.taskCard.priority_high')}
            </span>
          )}
        </div>

        {/* Second line — description, or the prompt when there is none */}
        {subtitle && (
          <p className="mt-1 text-xs text-muted line-clamp-2">{subtitle}</p>
        )}

        {/* Failure reason is the most useful thing a failed card can show */}
        {failed && latest?.error && (
          <p className="mt-1.5 flex items-start gap-1 text-[11px] text-danger line-clamp-2">
            <AlertCircle size={11} className="shrink-0 mt-0.5" />
            <span className="break-words">{latest.error}</span>
          </p>
        )}

        {/* Metadata row */}
        <div className="mt-2 flex items-center gap-2 text-[11px] text-muted flex-wrap">
          <span className="flex items-center gap-0.5">
            <Clock size={10} />
            {relativeTime(task.updated_at)}
          </span>

          {task.executions.length > 0 && (
            <span
              className="flex items-center gap-0.5"
              title={i18nT('apps.kanban.taskCard.run_count', { count: task.executions.length })}
            >
              <Play size={10} />
              {task.executions.length}
            </span>
          )}

          {task.tags.slice(0, 2).map((tag: string) => (
            <span key={tag} className="px-1.5 py-0.5 rounded bg-bg-hover text-[10px]">
              {tag}
            </span>
          ))}
        </div>

        {/* Action row — the card's own affordances, always reachable */}
        {(hasSession || (onRun && !running)) && (
          <div className="mt-2 pt-2 border-t border-border/60 flex items-center gap-2">
            {hasSession && onOpenSession && (
              <button
                className="flex items-center gap-1 text-[11px] text-accent hover:underline"
                onClick={e => { e.stopPropagation(); onOpenSession(latest!.session_key!) }}
                title={i18nT('apps.kanban.taskCard.open_agent_session')}
              >
                <MessageSquare size={11} />
                {running ? i18nT('apps.kanban.taskCard.watch_live') : i18nT('apps.kanban.taskCard.view_session')}
              </button>
            )}
            {onRun && !running && (
              <button
                className="ml-auto flex items-center gap-1 text-[11px] text-muted hover:text-accent transition-colors"
                onClick={e => { e.stopPropagation(); onRun(task) }}
                title={i18nT('apps.kanban.taskCard.run_this_task')}
              >
                <Play size={11} fill="currentColor" />
                {task.executions.length ? i18nT('apps.kanban.taskCard.run_again') : i18nT('apps.kanban.taskCard.run')}
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
