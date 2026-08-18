/** The main kanban board — horizontal columns with drag-and-drop. */
import { DndContext, DragOverlay, closestCorners, type DragEndEvent, type DragStartEvent } from '@dnd-kit/core'
import { useState } from 'react'
import type { TaskRecord, TaskStatus } from '../types'
import { COLUMNS, MANUAL_DROP_TARGETS } from '../types'
import { Column } from './Column'
import { TaskCard } from './TaskCard'

interface BoardProps {
  tasks: TaskRecord[]
  onMove: (taskId: string, newStatus: TaskStatus) => void
  onTaskClick: (task: TaskRecord) => void
  onTaskRun: (task: TaskRecord) => void
  onOpenSession: (sessionKey: string) => void
}

export function Board({ tasks, onMove, onTaskClick, onTaskRun, onOpenSession }: BoardProps) {
  const [activeTask, setActiveTask] = useState<TaskRecord | null>(null)

  const tasksByColumn = (status: TaskStatus) =>
    tasks.filter(t => t.status === status).sort((a, b) => b.updated_at - a.updated_at)

  function handleDragStart(event: DragStartEvent) {
    const task = tasks.find(t => t.id === event.active.id)
    if (task) setActiveTask(task)
  }

  function handleDragEnd(event: DragEndEvent) {
    setActiveTask(null)
    const { active, over } = event
    if (!over) return

    const taskId = active.id as string
    const task = tasks.find(t => t.id === taskId)
    if (!task) return

    // Determine which column was dropped on
    const overId = over.id as string
    let targetStatus: TaskStatus | null = null

    // If dropped on a column directly
    if (COLUMNS.some(c => c.id === overId)) {
      targetStatus = overId as TaskStatus
    } else {
      // Dropped on another card — find its column
      const overTask = tasks.find(t => t.id === overId)
      if (overTask) targetStatus = overTask.status
    }

    if (!targetStatus || targetStatus === task.status) return

    // Validate the move
    if (!MANUAL_DROP_TARGETS.includes(targetStatus)) return
    if (task.status === 'running' && !['done', 'failed'].includes(targetStatus)) return

    onMove(taskId, targetStatus)
  }

  return (
    <DndContext
      collisionDetection={closestCorners}
      onDragStart={handleDragStart}
      onDragEnd={handleDragEnd}
    >
      {/* Narrow-first: lanes stack into one column on a phone and become
          side-by-side rails from md up, so a 320px viewport shows a full lane
          instead of five clipped ones. */}
      <div className="flex flex-col md:flex-row gap-3 pb-4 h-full overflow-auto">
        {COLUMNS.map(column => (
          <Column
            key={column.id}
            column={column}
            tasks={tasksByColumn(column.id)}
            onTaskClick={onTaskClick}
            onTaskRun={onTaskRun}
            onOpenSession={onOpenSession}
          />
        ))}
      </div>

      {/* Drag overlay */}
      <DragOverlay>
        {activeTask ? (
          <div className="w-[280px] rotate-2 opacity-90">
            <TaskCard task={activeTask} onClick={() => {}} />
          </div>
        ) : null}
      </DragOverlay>
    </DndContext>
  )
}
