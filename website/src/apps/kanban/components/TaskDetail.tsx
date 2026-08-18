/** Task detail — a centered, roomy modal (not a right-edge drawer). */
import {
  ExternalLink, MessageSquare, Play, RotateCw, Trash2, X,
} from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import Clickable from '../../../components/Clickable'
import { fmtDateTimeNumeric, fmtDuration, fmtUnit } from '../../../i18n/format'
import { i18nT } from '../../../i18n/t'
import SimpleSelect from '../../../components/SimpleSelect'
import type { TaskRecord, TaskStatus } from '../types'
import { COLUMNS, MANUAL_DROP_TARGETS, laneLabel } from '../types'

interface TaskDetailProps {
  task: TaskRecord
  onClose: () => void
  onUpdate: (id: string, patch: Partial<TaskRecord>) => void
  onMove: (id: string, status: TaskStatus) => void
  onRun: (task: TaskRecord) => void
  onDelete: (id: string) => void
  onOpenSession: (sessionKey: string) => void
}

// Full literal keys indexed by the enum, never a key assembled from parts: a
// constructed key is invisible to extractors and unused-key tooling, which prune
// it and leave the raw key rendering in place of a sentence.
const RESULT_KEYS = {
  succeeded: 'apps.kanban.taskDetail.result_succeeded',
  failed: 'apps.kanban.taskDetail.result_failed',
  cancelled: 'apps.kanban.taskDetail.result_cancelled',
} as const

// fmtDateTimeNumeric, not toLocaleString(): the bare call reads the BROWSER's
// locale, so a translated UI would still render English dates. The numeric
// helper reproduces the platform default English rendering.
function formatTime(ts: number): string {
  return fmtDateTimeNumeric(ts * 1000)
}

// Each part goes through the locale's unit formatter rather than being pasted
// next to a literal suffix, so the digits, the unit and the separator between
// the two parts all follow the active locale.
function duration(startedAt: number, endedAt?: number | null): string {
  const end = endedAt ?? Date.now() / 1000
  const secs = Math.max(0, Math.round(end - startedAt))
  if (secs < 60) return fmtUnit(secs, 'second')
  if (secs < 3600) {
    return fmtDuration([[Math.floor(secs / 60), 'minute'], [secs % 60, 'second']])
  }
  return fmtDuration([
    [Math.floor(secs / 3600), 'hour'],
    [Math.floor((secs % 3600) / 60), 'minute'],
  ])
}

export function TaskDetail({
  task, onClose, onUpdate, onMove, onRun, onDelete, onOpenSession,
}: TaskDetailProps) {
  const [title, setTitle] = useState(task.title)
  const [description, setDescription] = useState(task.description)
  const [prompt, setPrompt] = useState(task.prompt)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [confirmDiscard, setConfirmDiscard] = useState(false)

  const col = COLUMNS.find(c => c.id === task.status)
  // A running card keeps its own lane listed so the trigger is never blank; the
  // control is disabled there anyway.
  const laneOptions: TaskStatus[] = task.status === 'running'
    ? ['running', ...MANUAL_DROP_TARGETS]
    : MANUAL_DROP_TARGETS
  const isDirty = title !== task.title || description !== task.description || prompt !== task.prompt

  // Latest execution — the one worth linking to.
  const latest = task.executions.length ? task.executions[task.executions.length - 1] : null

  // The parent passes the live record, so a background settle or the namer
  // landing a title arrives here on the board's next poll. Adopt such a change
  // ONLY for a field the user has not touched: a field that already diverges
  // from what was seeded is an in-progress edit, and overwriting it would lose
  // their typing to a background event they never saw.
  const seeded = useRef({ title: task.title, description: task.description, prompt: task.prompt })
  useEffect(() => {
    if (task.title !== seeded.current.title) {
      setTitle(current => (current === seeded.current.title ? task.title : current))
    }
    if (task.description !== seeded.current.description) {
      setDescription(current =>
        current === seeded.current.description ? task.description : current,
      )
    }
    if (task.prompt !== seeded.current.prompt) {
      setPrompt(current => (current === seeded.current.prompt ? task.prompt : current))
    }
    seeded.current = { title: task.title, description: task.description, prompt: task.prompt }
  }, [task.title, task.description, task.prompt])

  /**
   * Every close path funnels through here so an unsaved edit cannot vanish.
   * Escape and a click on the scrim are the two the user does not think of as
   * "closing", which is exactly why they were the ones that lost work.
   */
  const requestClose = useCallback(() => {
    if (isDirty) {
      setConfirmDiscard(true)
      return
    }
    onClose()
  }, [isDirty, onClose])

  // Escape asks to close; the guard decides whether it actually does.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') requestClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [requestClose])

  function handleSave() {
    if (!isDirty) return
    onUpdate(task.id, { title, description, prompt })
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-6">
      {/* Backdrop is a SIBLING of the dialog, never a wrapper — wrapping would
          put the dialog's own controls inside a role="button". Clickable gives
          the click-away dismissal a keyboard path and an accessible name. */}
      <Clickable
        className="absolute inset-0 bg-black/40 backdrop-blur-sm"
        onClick={requestClose}
        aria-label={i18nT('apps.kanban.taskDetail.close_detail')}
      />
      <div
        className="relative w-full max-w-3xl max-h-[85vh] bg-bg-elevated border border-border rounded-xl shadow-lg flex flex-col overflow-hidden"
        role="dialog"
        aria-modal="true"
        aria-label={i18nT('apps.kanban.taskDetail.task_detail')}
      >
        {/* Header */}
        <div className="flex items-center gap-3 px-2 md:px-6 py-4 border-b border-border">
          {/* The lane is a CONTROL, not a badge. Drag is pointer-only -- its
              listeners live on the card's grip -- so on a touch screen this
              select is the only way to file a card, and it is what makes the
              board's own phone layout able to organise rather than just create.
              Disabled while a run owns the lane: the endpoint refuses that move
              (409 task_is_running) until the watcher settles it.
              SimpleSelect, not a native <select>: an OS-drawn popup ignores the
              theme (see website/docs/page-layout.md). */}
          <SimpleSelect
            id="kanban-detail-lane"
            aria-label={i18nT('apps.kanban.taskDetail.move_to')}
            value={task.status}
            options={laneOptions}
            optionLabels={laneOptions.map(laneLabel)}
            onChange={next => onMove(task.id, next as TaskStatus)}
            disabled={task.status === 'running'}
            className={`px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide ${col?.bgSubtle ?? 'bg-bg-hover'} ${col?.textColor ?? 'text-muted'}`}
          />
          <span className="text-[11px] text-muted ml-auto">
            {i18nT('apps.kanban.taskDetail.updated')} {formatTime(task.updated_at)}
          </span>
          <button
            className="p-1.5 rounded hover:bg-bg-hover text-muted hover:text-text"
            onClick={requestClose}
            aria-label={i18nT('apps.kanban.taskDetail.close')}
          >
            <X size={16} />
          </button>
        </div>

        {/* Body — two columns on wide screens */}
        <div className="flex-1 overflow-y-auto p-6">
          <div className="grid grid-cols-1 lg:grid-cols-[1.4fr_1fr] gap-6">
            {/* Left: the editable task */}
            <div className="space-y-4 min-w-0">
              <div>
                <label className="text-[11px] font-medium text-muted uppercase tracking-wide">{i18nT('apps.kanban.taskDetail.title_label')}</label>
                <input
                  className="mt-1 w-full bg-bg border border-border rounded-md px-3 py-2 text-sm text-text-strong focus:outline-none focus:ring-1 focus:ring-accent"
                  value={title}
                  onChange={e => setTitle(e.target.value)}
                />
              </div>

              <div>
                <label className="text-[11px] font-medium text-muted uppercase tracking-wide">{i18nT('apps.kanban.taskDetail.description_label')}</label>
                <textarea
                  className="mt-1 w-full bg-bg border border-border rounded-md px-3 py-2 text-sm text-text min-h-[70px] resize-y focus:outline-none focus:ring-1 focus:ring-accent"
                  value={description}
                  onChange={e => setDescription(e.target.value)}
                  placeholder={i18nT('apps.kanban.taskDetail.description_placeholder')}
                />
              </div>

              <div>
                <label className="text-[11px] font-medium text-muted uppercase tracking-wide">{i18nT('apps.kanban.taskDetail.execution_prompt_label')}</label>
                <textarea
                  className="mt-1 w-full bg-bg border border-border rounded-md px-3 py-2 text-sm text-text font-mono min-h-[140px] resize-y focus:outline-none focus:ring-1 focus:ring-accent"
                  value={prompt}
                  onChange={e => setPrompt(e.target.value)}
                  placeholder={i18nT('apps.kanban.taskDetail.prompt_placeholder')}
                />
              </div>

              {isDirty && (
                <button
                  className="w-full py-2 rounded-md bg-accent text-accent-fg text-sm font-medium hover:bg-accent-hover transition-colors"
                  onClick={handleSave}
                >
                  {i18nT('apps.kanban.taskDetail.save_changes')}
                </button>
              )}
            </div>

            {/* Right: session and history */}
            <div className="space-y-5 min-w-0">
              {/* Jump to the live/most recent session */}
              {latest?.session_key && (
                <div>
                  <div className="text-[11px] font-medium text-muted uppercase tracking-wide mb-2">{i18nT('apps.kanban.taskDetail.session')}</div>
                  <button
                    className="w-full flex items-center gap-2 px-3 py-2.5 rounded-md bg-accent-subtle border border-accent/30 text-accent text-sm font-medium hover:bg-accent/20 transition-colors"
                    onClick={() => onOpenSession(latest.session_key!)}
                  >
                    <MessageSquare size={14} />
                    {i18nT('apps.kanban.taskDetail.open_agent_session')}
                    <ExternalLink size={12} className="ml-auto opacity-70" />
                  </button>
                  <p className="mt-1.5 text-[10px] text-muted">
                    {task.status === 'running'
                      ? i18nT('apps.kanban.taskDetail.agent_working')
                      : i18nT('apps.kanban.taskDetail.read_transcript')}
                  </p>
                </div>
              )}

              {/* Execution history */}
              {task.executions.length > 0 && (
                <div>
                  <div className="flex items-center gap-2 mb-2">
                    <Play size={13} className="text-accent" />
                    <span className="text-[11px] font-medium text-muted uppercase tracking-wide">
                      {i18nT('apps.kanban.taskDetail.runs')} ({task.executions.length})
                    </span>
                  </div>
                  <div className="space-y-1.5 max-h-[180px] overflow-y-auto">
                    {[...task.executions].reverse().map(exec => (
                      <div
                        key={exec.id}
                        className="flex items-center gap-2 px-2.5 py-1.5 rounded bg-bg border border-border text-[11px]"
                      >
                        <span className={`shrink-0 w-1.5 h-1.5 rounded-full ${
                          exec.result === 'succeeded' ? 'bg-ok'
                            : exec.result === 'failed' ? 'bg-danger'
                            : exec.result === 'cancelled' ? 'bg-muted'
                            : 'bg-warn animate-pulse'
                        }`} />
                        <span className={`font-medium shrink-0 ${
                          exec.result === 'succeeded' ? 'text-ok'
                            : exec.result === 'failed' ? 'text-danger'
                            : exec.result === 'cancelled' ? 'text-muted'
                            : 'text-warn'
                        }`}>
                          {exec.result
                            ? i18nT(RESULT_KEYS[exec.result])
                            : i18nT('apps.kanban.taskDetail.running')}
                        </span>
                        <span className="text-muted truncate">
                          {duration(exec.started_at, exec.ended_at)}
                        </span>
                        {exec.session_key && (
                          <button
                            className="ml-auto shrink-0 text-accent hover:underline flex items-center gap-0.5"
                            onClick={() => onOpenSession(exec.session_key!)}
                          >
                            <ExternalLink size={9} />
                            {i18nT('apps.kanban.taskDetail.open')}
                          </button>
                        )}
                      </div>
                    ))}
                  </div>
                  {task.executions.some(e => e.error) && (
                    <p className="mt-1.5 text-[10px] text-danger break-words">
                      {[...task.executions].reverse().find(e => e.error)?.error}
                    </p>
                  )}
                </div>
              )}
            </div>
          </div>
        </div>

        {/* Footer actions */}
        <div className="border-t border-border px-2 md:px-6 py-3 flex gap-2">
          {task.status !== 'running' ? (
            <button
              className="flex items-center justify-center gap-1.5 px-5 py-2 rounded-md bg-accent text-accent-fg text-sm font-medium hover:bg-accent-hover transition-colors"
              onClick={() => onRun(task)}
            >
              {task.executions.length > 0 ? <RotateCw size={14} /> : <Play size={14} />}
              {task.executions.length > 0 ? i18nT('apps.kanban.taskDetail.run_again') : i18nT('apps.kanban.taskDetail.run')}
            </button>
          ) : (
            <span className="flex items-center gap-1.5 px-3 py-2 text-xs text-warn">
              <span className="w-1.5 h-1.5 rounded-full bg-warn animate-pulse" />
              {i18nT('apps.kanban.taskDetail.running_pill')}
            </span>
          )}
          <button
            className="ml-auto px-4 py-2 rounded-md bg-danger-subtle text-danger text-sm font-medium hover:bg-danger/20 transition-colors"
            onClick={() => {
              if (confirmDelete) onDelete(task.id)
              else setConfirmDelete(true)
            }}
          >
            <Trash2 size={14} className="inline mr-1" />
            {confirmDelete ? i18nT('apps.kanban.taskDetail.really_delete') : i18nT('apps.kanban.taskDetail.delete')}
          </button>
        </div>

        {/* Discard guard. Rendered inside the dialog rather than as a native
            confirm() so it is reachable, themed, and cannot be suppressed by a
            browser that blocks dialogs. */}
        {confirmDiscard && (
          <div className="flex items-center gap-3 px-2 md:px-6 py-3 border-t border-border bg-warn-subtle" role="alertdialog" aria-label={i18nT('apps.kanban.taskDetail.unsaved_changes')}>
            <span className="flex-1 text-xs text-text">
              {i18nT('apps.kanban.taskDetail.unsaved_changes_discard_prompt')}
            </span>
            <button
              className="px-3 py-1.5 rounded-md bg-bg-hover text-text text-xs font-medium hover:bg-bg-elevated transition-colors"
              onClick={() => setConfirmDiscard(false)}
            >
              {i18nT('apps.kanban.taskDetail.keep_editing')}
            </button>
            <button
              className="px-3 py-1.5 rounded-md bg-danger-subtle text-danger text-xs font-medium hover:bg-danger/20 transition-colors"
              onClick={onClose}
            >
              {i18nT('apps.kanban.taskDetail.discard')}
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
