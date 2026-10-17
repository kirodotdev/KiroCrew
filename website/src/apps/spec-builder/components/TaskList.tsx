// TaskList — tasks.md rendered as addressable work rather than static markdown.
//
// The app could previously only hand the WHOLE list to an autonudge loop: there
// was no way to run one task, no way to see which task a run was on, and no
// progress anywhere. The checkboxes were markdown, so they showed state without
// offering any.
//
// Everything here is DERIVED from tasks.md, which stays the source of truth. That
// file is the interop contract with the Kiro IDE and CLI, so a sidecar task store
// would have bought addressability by breaking the thing this app gets right — and
// a box checked by the agent, or by hand in an editor, shows up here on the next
// poll without anything having to be told.
import { CheckCircle2, Circle, Play, ListChecks } from 'lucide-react'
import { motion } from 'framer-motion'
import type { SpecTask } from '../api'
import { ACCENT, SEL_BG, PULSE_MOTION, Btn } from './shared'

import { i18nT } from '../../../i18n/t'
import { fmtPercent } from '../../../i18n/format'

export interface TaskListProps {
  tasks: SpecTask[]
  progress?: { done: number; total: number }
  /** Index currently being dispatched, or null. */
  pendingIndex: number | null
  /** True while an autonudge loop is working the whole list, or a turn is in
   *  flight. Single-task runs are refused then, so the controls say why instead
   *  of failing on click. */
  busy: boolean
  onRun: (task: SpecTask) => void
}

export default function TaskList({ tasks, progress, pendingIndex, busy, onRun }: TaskListProps) {
  if (!tasks.length) {
    return (
      <div className="h-full flex flex-col items-center justify-center gap-2.5 text-center px-6">
        <ListChecks size={26} strokeWidth={1.5} className="text-muted opacity-50" />
        <div className="text-[13px] text-muted max-w-[420px] leading-relaxed">
          {i18nT('apps.specBuilder.components.taskList.no_tasks_yet')}
        </div>
      </div>
    )
  }

  const done = progress?.done ?? tasks.filter((t) => t.done).length
  const total = progress?.total ?? tasks.length
  // Kept as a ratio for display, and separately as a whole number for the CSS
  // width: the readable label goes through fmtPercent so the digits and the sign
  // are the locale's, while the bar's width has to stay bare digits to be a
  // length. The two must not be collapsed into one value.
  const ratio = total ? done / total : 0
  const pctWidth = Math.round(ratio * 100)

  return (
    <div className="flex flex-col min-h-0">
      {/* Progress header. A determinate bar rather than a spinner: the counter in
          the CONTEXT card only ever showed turns and tool calls, so during a long
          build the user watched a number climb with no idea how far along the plan
          it was. */}
      <div className="shrink-0 px-4 pt-3.5 pb-2.5 border-b border-border">
        <div className="flex items-center gap-2 mb-1.5">
          <span className="text-[12px] font-semibold text-text">
            {i18nT('apps.specBuilder.components.taskList.done_of_total', { done, total })}
          </span>
          <span className="flex-1" />
          <span className="text-[11px] font-mono text-muted">{fmtPercent(ratio)}</span>
        </div>
        <div
          className="h-1.5 rounded-full overflow-hidden"
          style={{ background: 'var(--border)' }}
          role="progressbar"
          aria-valuenow={done}
          aria-valuemin={0}
          aria-valuemax={total}
          aria-label={i18nT('apps.specBuilder.components.taskList.task_progress')}
        >
          <div className="h-full rounded-full transition-all" style={{ width: pctWidth + '%', background: ACCENT }} />
        </div>
      </div>

      <ul className="flex-1 min-h-0 overflow-y-auto px-2 py-1.5 list-none m-0">
        {tasks.map((task) => {
          const pending = pendingIndex === task.index
          return (
            <li
              key={task.index + ':' + task.hash}
              className="flex gap-2.5 items-start px-2 py-2 border-b border-border last:border-b-0"
            >
              <span className="shrink-0 mt-[1px]">
                {task.done
                  ? <CheckCircle2 size={15} strokeWidth={2} style={{ color: 'var(--ok)' }} />
                  : pending
                    ? <motion.span className="block w-[13px] h-[13px] m-[1px] rounded-full" style={{ background: ACCENT }} {...PULSE_MOTION} />
                    : <Circle size={15} strokeWidth={1.75} className="text-muted" />}
              </span>
              <span
                className={`flex-1 min-w-0 text-[13px] leading-snug ${
                  task.done ? 'text-muted line-through' : 'text-text'}`}
              >
                {task.text}
              </span>
              {task.done
                ? (
                  <span
                    className="text-[10px] font-bold px-1.5 py-0.5 rounded-full shrink-0 uppercase tracking-wide"
                    style={{ color: ACCENT, background: SEL_BG }}
                  >
                    {i18nT('apps.specBuilder.components.taskList.done')}
                  </span>
                )
                : (
                  // Disabled while ANY turn is in flight, not just this one: the
                  // backend refuses a single-task run during a whole-list build
                  // because both write the same files and check the same boxes,
                  // so the control explains that instead of failing on click.
                  <Btn
                    label={<><Play className="lucide-inline" /> {pending
                      ? i18nT('apps.specBuilder.components.taskList.starting')
                      : i18nT('apps.specBuilder.components.taskList.run')}</>}
                    disabled={busy || pendingIndex !== null}
                    ariaLabel={i18nT('apps.specBuilder.components.taskList.run_task', { task: task.text })}
                    title={busy
                      ? i18nT('apps.specBuilder.components.taskList.pause_the_build_first')
                      : i18nT('apps.specBuilder.components.taskList.run_only_this_task_and_stop')}
                    onClick={() => onRun(task)}
                  />
                )}
            </li>
          )
        })}
      </ul>
    </div>
  )
}
