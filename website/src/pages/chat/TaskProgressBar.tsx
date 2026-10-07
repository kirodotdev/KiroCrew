import { useMemo, useCallback, useState, useEffect, memo } from 'react'
import { ListTodo, ChevronDown, ChevronRight, CheckSquare2, Square, CheckCircle2, Circle, UserCheck, UserX } from 'lucide-react'
import { useMutation } from '@tanstack/react-query'
import { useAppSelector } from '../../store'
import { api, ApiError } from '../../api/client'
import { queryClient } from '../../api/queryClient'
import ErrorNotice from '../../components/ErrorNotice'
import { sanitizeLlmOutput } from '../../utils/sanitize'
import type { TodoList } from '../../types'
import { useRowDisclosure } from './rowDisclosure'
import { Glass } from '../../components/Glass'

import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
/** Rows rendered before the list scrolls internally — bounds DOM on long plans. */
const MAX_VISIBLE_ROWS = 12

/** The gateway's refusal `code` for a tick, read off the error body. */
function tickFailureCode(e: unknown): string | null {
  if (!(e instanceof ApiError) || !e.body) return null
  try {
    const code = (JSON.parse(e.body) as { code?: unknown }).code
    return typeof code === 'string' ? code : null
  } catch {
    return null
  }
}

/**
 * The refusal in the person's words, or null when the code has no plain
 * rendering. Only the codes the todo route itself answers are mapped; a
 * fence refusal shared with the other slot writes falls through to its text.
 */
function tickFailureDetail(e: unknown): string | null {
  switch (tickFailureCode(e)) {
    case 'todo_task_stale':
      return i18nT('pages.chat.taskProgressBar.fail_task_stale')
    case 'todo_task_not_found':
    case 'todo_absent':
      return i18nT('pages.chat.taskProgressBar.fail_task_gone')
    case 'caller_unattributable':
      return i18nT('pages.chat.taskProgressBar.fail_session_gone')
    case 'relay_archive_read_only':
      return i18nT('pages.chat.taskProgressBar.fail_remote')
    default:
      return null
  }
}

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
 *
 * Each row is a toggle. The agent's own list lives inside its native
 * conversation, which the gateway replaces on an agent switch, a failed resume
 * or `/clear`; the pill's snapshot survives that, so the agent can no longer
 * tick the rows it shows. A click writes the dashboard's copy through
 * `PATCH /api/chat/slots/{slot}/todo`; the gateway echoes the same
 * `todo_update` delta the tool result does, so the store repaints and the next
 * fresh agent session rebuilds its list from this copy.
 */
const TaskProgressBar = memo(function TaskProgressBar({ slot, disclosureKey }: { slot: string | null; disclosureKey?: string }) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const [expanded, setExpanded] = useRowDisclosure(disclosureKey, false)
  // Select the primitive-bearing todo object for this slot only, so unrelated
  // slot churn in the slots array doesn't re-render the pill.
  const todo = useAppSelector(s =>
    (s.dashboard.slots ?? []).find(x => x.key === slot)?.todo ?? null
  ) as TodoList | null
  // A remote-bound session's checklist is a relay of the peer's: the local slot
  // holds no list to write, so the PATCH would 404. Rows stay read-only there.
  const editable = useAppSelector(s =>
    (s.dashboard.slots ?? []).find(x => x.key === slot)?.executor !== 'remote'
  )

  const tasks = useMemo(() => todo?.tasks ?? [], [todo])
  const toggle = useCallback(() => setExpanded(v => !v), [setExpanded])
  // The row repaints ONLY from the gateway's `todo_update` echo (and the
  // `slots` snapshot on reconnect), which the gateway serializes. Applying the
  // PATCH response here too would let two quick clicks land out of order and
  // an older response restore an obsolete list. A refused or lost PATCH is
  // SAID, or the click reads as "nothing happened", the exact complaint this
  // control exists to fix.
  //
  // Clicks are QUEUED and sent one at a time: a second in-flight mutate would
  // replace the observed one and swallow its failure, but freezing every row
  // for the round trip would drop clicks on a slow link. The queue drains on
  // each settle; a failure clears the rest so a person is not left with edits
  // they cannot see landing. Every queued item carries the slot it was clicked
  // in: the queue is component state that outlives a session switch, and task
  // ids are positional, so PATCHing the CURRENT slot would tick a same-numbered
  // row in whichever conversation the person switched to.
  // The row's TEXT travels with the click: ids are positional and the agent
  // can replace the list under a click, so the gateway refuses a tick whose id
  // now names a different task instead of ticking the wrong one.
  type Tick = { slot: string; id: string; text: string; completed: boolean }
  const [queue, setQueue] = useState<Tick[]>([])
  // Failures are kept PER SLOT, independent of react-query's single shared
  // mutation state. `tick.error` holds only the LAST mutation's error and is
  // reset the moment the next queued tick (for any slot) starts, so reading the
  // notice off it made slot A's failure vanish the instant slot B's queued tick
  // mutated. This map outlives that reset: A's failure stays until A itself is
  // retried or dismissed.
  // Each failure keeps the text of the row that was clicked: after a tick that
  // landed and one that did not, the notice must say WHICH row kept its state.
  const [failures, setFailures] = useState<Record<string, { error: unknown; text: string }>>({})
  const tick = useMutation({
    mutationFn: ({ slot: target, id, text, completed }: Tick) =>
      api.setTodoTask(target, id, text, completed),
    // Only THIS click's session loses its queue: a 409 on session A must not
    // drop what the person queued in session B while the component (which
    // does not remount on a switch) held both. The failure is also recorded
    // under its own slot so the notice survives another slot's later mutate.
    onError: (e, vars) => {
      setQueue(q => q.filter(x => x.slot !== vars.slot))
      setFailures(f => ({ ...f, [vars.slot]: { error: e, text: vars.text } }))
    },
    // A slot's own successful tick clears its retained failure.
    onSuccess: (_d, vars) => setFailures(f => {
      if (!(vars.slot in f)) return f
      const { [vars.slot]: _drop, ...rest } = f
      return rest
    }),
  }, queryClient)
  // Not gated on isError: onError already emptied the queue, and starting a new
  // tick for a slot clears that slot's retained failure, so a click after a
  // refusal is sent rather than greyed out under the previous click's notice.
  useEffect(() => {
    if (tick.isPending || queue.length === 0) return
    const [next, ...rest] = queue
    setQueue(rest)
    setFailures(f => {
      if (!(next.slot in f)) return f
      const { [next.slot]: _drop, ...restF } = f
      return restF
    })
    tick.mutate(next)
  }, [queue, tick.isPending, tick.isError, tick])
  // Only THIS slot's pending rows are held; a switch away and back must not
  // show another session's queue on these rows.
  const pendingIds = useMemo(() => {
    const ids = new Set(queue.filter(q => q.slot === slot).map(q => q.id))
    if (tick.isPending && tick.variables && tick.variables.slot === slot) ids.add(tick.variables.id)
    return ids
  }, [queue, slot, tick.isPending, tick.variables])
  const enqueue = useCallback((id: string, text: string, completed: boolean) => {
    if (!slot) return
    const target = slot
    setQueue(q => q.some(x => x.slot === target && x.id === id) ? q : [...q, { slot: target, id, text, completed }])
  }, [slot])
  // Shown only in the session the failed click belonged to. The gateway's
  // refusal codes are named for its own log; the person needs the reason in
  // their words (what happened to THEIR click), so a known code is rendered as
  // a plain sentence and the server's text is kept on the tooltip. An unknown
  // code, or a transport failure with no code, shows the raw text.
  // Shown only in the session the failed click belonged to, read from the
  // per-slot map so another slot's later mutate cannot erase it.
  const failed = slot ? (failures[slot] ?? null) : null
  const failure = failed?.error ?? null
  const rawError = failure ? (failure instanceof Error ? failure.message : String(failure)) : null
  const errorDetail = failure ? tickFailureDetail(failure) : null
  const failedTask = failed ? sanitizeLlmOutput(failed.text) : ''
  const dismissFailure = useCallback(() => {
    tick.reset()
    if (!slot) return
    setFailures(f => {
      if (!(slot in f)) return f
      const { [slot]: _drop, ...rest } = f
      return rest
    })
  }, [slot, tick])

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
    // `relative z-[2]` clears the transcript's bottom mask (`z-[1]`), which
    // overshoots below the scrollport edge for a composer status stack it assumes
    // is empty. Whenever this bar is the topmost thing in that stack, an auto
    // z-index let the mask's opaque tail shave its top border and corners.
    <div className="px-4 mx-auto w-full relative z-[2]" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
      {/* Collapsed = a small pill that hugs its content; expanded = a full-width
          panel. Keeping the collapsed state inline stops it reading as another
          full-width bar competing with the composer below it. Both states are
          the dock's glass (components/Glass.tsx) on the accent tint step; the
          pill takes the follow-up chips' radius, the panel the bars' radius;
          `thick` so the progress rows stay readable over the transcript (#16299). */}
      <Glass
        variant="chip"
        thickness="thick"
        radius={expanded ? 8 : 16}
        className={`mb-1 animate-slide-up glass-accent ${
          expanded ? '' : 'inline-flex max-w-full'
        }`}
      >
        {/* The clip lives one level in, not on the pane: the pane's hairlines sit
            half a pixel OUTSIDE its top and bottom edges, and `overflow: hidden`
            on the pane itself would cut them (see QuestionCard). The inner box
            inherits the radius so the row hover fills still stop at the arc. */}
        <div className="min-w-0 overflow-hidden rounded-[inherit]">
        <button
          type="button"
          data-testid="todo-pill"
          onClick={toggle}
          aria-expanded={expanded}
          aria-label={expanded
            ? i18nT('pages.chat.taskProgressBar.aria_collapse_task_list', { done, total })
            : i18nT('pages.chat.taskProgressBar.aria_expand_task_list', { done, total })}
          className={`flex items-center gap-2 py-1.5 text-[13px] font-mono bg-transparent border-none cursor-pointer hover:bg-accent/5 transition-colors focus-visible:outline-hidden focus-visible:ring-1 focus-visible:ring-accent ${
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
            {/* Said once, at rest: the rows below are controls, and a click is
                reversible. Without this the checkbox glyphs read as the agent's
                own status marks nobody dares press. */}
            <li className="text-[11px] text-muted/70 font-mono pb-1" data-testid="todo-hint">
              {editable
                ? i18nT('pages.chat.taskProgressBar.click_to_toggle_hint')
                : i18nT('pages.chat.taskProgressBar.read_only_hint')}
            </li>
            {tasks.slice(0, MAX_VISIBLE_ROWS).map((t, i) => {
              const id = t.id || String(i + 1)
              const text = sanitizeLlmOutput(t.text || '')
              const label = t.completed
                ? i18nT('pages.chat.taskProgressBar.aria_mark_not_done', { task: text })
                : i18nT('pages.chat.taskProgressBar.aria_mark_done', { task: text })
              // Whose mark is this: the agent finished it, or a person told the
              // agent so? The two look alike as a bare checkbox, so a row the
              // person set carries its own glyph and says so in the tooltip
              // until the agent's own list agrees.
              const byPerson = !!t.person
              const title = byPerson
                ? `${i18nT(t.completed ? 'pages.chat.taskProgressBar.set_by_you_done' : 'pages.chat.taskProgressBar.set_by_you_open')} ${label}`
                : label
              if (!editable) {
                return (
                  <li key={id} data-testid="todo-row" className="flex items-start gap-1.5 text-[12px] font-mono">
                    {/* Circle glyphs, not checkboxes: a status mark, not a control. */}
                    {t.completed
                      ? <CheckCircle2 size={12} className="mt-[3px] shrink-0 text-ok" aria-hidden="true" />
                      : <Circle size={12} className="mt-[3px] shrink-0 text-muted/50" aria-hidden="true" />}
                    <span className={t.completed ? 'text-muted/60 line-through' : 'text-text'}>{text}</span>
                  </li>
                )
              }
              return (
                <li key={id} data-testid="todo-row" className="text-[12px] font-mono">
                  <button
                    type="button"
                    role="checkbox"
                    aria-checked={!!t.completed}
                    aria-label={title}
                    title={title}
                    disabled={pendingIds.has(id)}
                    onClick={() => enqueue(id, t.text || '', !t.completed)}
                    data-testid="todo-row-toggle"
                    className="flex w-full items-start gap-1.5 text-left bg-transparent border-none px-1 py-0.5 -mx-1 cursor-pointer rounded-sm hover:bg-accent/10 disabled:cursor-default disabled:opacity-60 focus-visible:outline-hidden focus-visible:ring-1 focus-visible:ring-accent"
                  >
                    {/* A held row keeps its glyph, dimmed in place (the button's
                        disabled opacity): a spinner here read as the AGENT
                        working on the row. A person-set row wears a person
                        glyph so it cannot be mistaken for the agent's own
                        completion. */}
                    {byPerson
                      ? t.completed
                        ? <UserCheck size={13} className="mt-[2px] shrink-0 text-ok" aria-hidden="true" data-testid="todo-row-person" />
                        : <UserX size={13} className="mt-[2px] shrink-0 text-accent/70" aria-hidden="true" data-testid="todo-row-person" />
                      : t.completed
                        ? <CheckSquare2 size={13} className="mt-[2px] shrink-0 text-ok" aria-hidden="true" />
                        : <Square size={13} className="mt-[2px] shrink-0 text-accent/70" aria-hidden="true" />}
                    <span className={t.completed ? 'text-muted/60 line-through' : 'text-text'}>
                      {text}
                    </span>
                    {/* Visible, not just announced: the dimmed row alone read
                        as a hover, so a slow link looked like an ignored click. */}
                    {pendingIds.has(id) && <span className="ml-auto shrink-0 pl-2 text-[11px] text-muted/70 italic" data-testid="todo-row-pending">{i18nT('pages.chat.taskProgressBar.sending')}</span>}
                  </button>
                </li>
              )
            })}
            {tasks.length > MAX_VISIBLE_ROWS && (
              <li className="text-[11px] text-muted/60 font-mono pl-[18px]">
                + {tasks.length - MAX_VISIBLE_ROWS} {i18nT('pages.chat.taskProgressBar.more')}
              </li>
            )}
          </ul>
        )}
        {/* Outside the scrolling list on purpose: a refusal must be readable
            wherever the list is scrolled, not clipped by its max-height. */}
        {expanded && rawError && (
          <div className="px-3 pb-2 font-mono">
            <ErrorNotice
              variant="block"
              message={errorDetail ?? rawError}
              messageTooltip={errorDetail ? rawError ?? undefined : undefined}
              messagePlacement="below"
              actionPlacement="below"
              title={i18nT('pages.chat.taskProgressBar.tick_failed_lead', { task: failedTask })}
              askAgent
              onDismiss={dismissFailure}
              testId="todo-tick-error"
            />
          </div>
        )}
        </div>
      </Glass>
    </div>
  )
})

export default TaskProgressBar
