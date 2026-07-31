// The action-item sidebar of a live meeting.
//
// Shows what the task extractor has pulled out so far, lets the user fix a
// description or an assignee in place, and quick-adds one the agent missed.
// Filing happens in the review view after the meeting ends — this panel is for
// keeping the list honest while the meeting is still running.

import { useRef } from 'react'
import { Plus, Trash2, X } from 'lucide-react'

import { i18nT } from '../../../i18n/t'
import { Badge, Btn, Input, Select, SendBtn } from '../../../components/ui'
import { PRIORITY_LABEL_KEY, type Task, type TaskPriority } from '../api'

const PRIORITIES: TaskPriority[] = ['high', 'medium', 'low']

function priorityBadge(priority: TaskPriority): 'err' | 'warn' | 'muted' {
  if (priority === 'high') return 'err'
  if (priority === 'medium') return 'warn'
  return 'muted'
}

interface Props {
  tasks: Task[]
  onClose: () => void
  onAdd: (description: string) => void
  onUpdate: (taskId: string, fields: Partial<Task>) => void
  onDelete: (taskId: string) => void
}

export default function TaskSidebar({ tasks, onClose, onAdd, onUpdate, onDelete }: Props) {
  const quickAddRef = useRef<HTMLInputElement>(null)

  const quickAdd = () => {
    const description = quickAddRef.current?.value.trim()
    if (!description) return
    onAdd(description)
    if (quickAddRef.current) quickAddRef.current.value = ''
  }

  const open = tasks.filter(task => task.review_status !== 'archived')

  return (
    <aside
      className="flex-none w-[340px] border-l border-border bg-bg flex flex-col overflow-hidden"
      aria-label={i18nT('apps.meetings.taskSidebar.title')}
    >
      <div className="flex items-center gap-2 px-4 py-3 border-b border-border">
        <span className="flex-1 font-medium text-sm text-text">
          {i18nT('apps.meetings.taskSidebar.heading', { count: open.length })}
        </span>
        <Btn onClick={onClose} aria-label={i18nT('apps.meetings.taskSidebar.close')}>
          <X className="lucide-inline" />
        </Btn>
      </div>

      <div className="flex-1 overflow-y-auto p-3 flex flex-col gap-2">
        {open.length === 0 ? (
          <p className="text-[13px] text-muted text-center mt-8">
            {i18nT('apps.meetings.taskSidebar.empty')}
          </p>
        ) : (
          open.map(task => (
            <div
              key={task.id}
              className="p-3 border border-border rounded-md flex flex-col gap-2 bg-card"
            >
              <Input
                defaultValue={task.description}
                aria-label={i18nT('apps.meetings.taskSidebar.descriptionLabel')}
                onBlur={e => {
                  const next = e.target.value.trim()
                  if (next && next !== task.description) onUpdate(task.id, { description: next })
                }}
              />
              <div className="flex items-center gap-2">
                <Input
                  defaultValue={task.assignee}
                  placeholder={i18nT('apps.meetings.taskSidebar.unassigned')}
                  aria-label={i18nT('apps.meetings.taskSidebar.assigneeLabel')}
                  className="flex-1"
                  onBlur={e => {
                    if (e.target.value.trim() !== task.assignee) {
                      onUpdate(task.id, { assignee: e.target.value.trim() })
                    }
                  }}
                />
                <Select
                  value={task.priority}
                  aria-label={i18nT('apps.meetings.taskSidebar.priorityLabel')}
                  onChange={e => onUpdate(task.id, { priority: e.target.value as TaskPriority })}
                >
                  {PRIORITIES.map(priority => (
                    <option key={priority} value={priority}>
                      {i18nT(PRIORITY_LABEL_KEY[priority])}
                    </option>
                  ))}
                </Select>
              </div>
              <div className="flex items-center gap-2">
                <Badge variant={priorityBadge(task.priority)}>
                  {i18nT(PRIORITY_LABEL_KEY[task.priority])}
                </Badge>
                {task.review_status === 'pushed' && (
                  <Badge variant="ok">{i18nT('apps.meetings.taskSidebar.filed')}</Badge>
                )}
                <Btn
                  danger
                  className="ml-auto"
                  onClick={() => onDelete(task.id)}
                  aria-label={i18nT('apps.meetings.taskSidebar.deleteTask')}
                >
                  <Trash2 className="lucide-inline" />
                </Btn>
              </div>
            </div>
          ))
        )}
      </div>

      <div className="flex-none border-t border-border px-4 py-3 flex items-center gap-2">
        <Input
          ref={quickAddRef}
          type="text"
          className="flex-1"
          placeholder={i18nT('apps.meetings.taskSidebar.quickAddPlaceholder')}
          aria-label={i18nT('apps.meetings.taskSidebar.quickAddPlaceholder')}
          onKeyDown={e => {
            if (e.key === 'Enter') quickAdd()
          }}
        />
        <SendBtn onClick={quickAdd} aria-label={i18nT('apps.meetings.taskSidebar.add')}>
          <Plus className="lucide-inline" />
          {i18nT('apps.meetings.taskSidebar.add')}
        </SendBtn>
      </div>
    </aside>
  )
}
