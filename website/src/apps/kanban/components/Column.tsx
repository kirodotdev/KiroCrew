/** A single kanban column (drop zone + card list). */
import { useDroppable } from '@dnd-kit/core'
import { i18nT } from '../../../i18n/t'
import { SortableContext, verticalListSortingStrategy } from '@dnd-kit/sortable'
import type { TaskRecord } from '../types'
import { laneLabel } from '../types'
import type { ColumnDef } from '../types'
import { TaskCard } from './TaskCard'

interface ColumnProps {
  column: ColumnDef
  tasks: TaskRecord[]
  onTaskClick: (task: TaskRecord) => void
  onTaskRun: (task: TaskRecord) => void
  onOpenSession: (sessionKey: string) => void
}

export function Column({ column, tasks, onTaskClick, onTaskRun, onOpenSession }: ColumnProps) {
  const { setNodeRef, isOver } = useDroppable({ id: column.id })

  return (
    <div className="flex flex-col w-full md:w-[280px] md:min-w-[280px] md:shrink-0">
      {/* Column header */}
      <div className={`flex items-center gap-2 px-3 py-2 rounded-t-lg ${column.bgSubtle}`}>
        <div className={`w-2 h-2 rounded-full bg-${column.color}`} />
        <h3 className={`text-xs font-semibold uppercase tracking-wide ${column.textColor}`}>
          {laneLabel(column.id)}
        </h3>
        <span className="text-[11px] text-muted font-medium ml-auto tabular-nums">
          {tasks.length}
        </span>
      </div>

      {/* Card list (scrollable) */}
      <div
        ref={setNodeRef}
        className={`
          flex-1 flex flex-col gap-2 p-2 rounded-b-lg
          border border-t-0 border-border bg-bg overflow-y-auto
          min-h-[200px] transition-colors duration-150
          ${isOver ? 'bg-accent-subtle/30 border-accent/40' : ''}
        `}
      >
        <SortableContext items={tasks.map(t => t.id)} strategy={verticalListSortingStrategy}>
          {tasks.map(task => (
            <TaskCard
              key={task.id}
              task={task}
              onClick={onTaskClick}
              onRun={column.id === 'running' ? undefined : onTaskRun}
              onOpenSession={onOpenSession}
            />
          ))}
        </SortableContext>

        {tasks.length === 0 && (
          <div className="flex-1 flex items-center justify-center min-h-[80px]">
            <p className="text-xs text-muted italic">{i18nT('apps.kanban.column.no_tasks')}</p>
          </div>
        )}
      </div>
    </div>
  )
}
