// The action-item sidebar of a live meeting.
//
// Shows what the task extractor has pulled out so far, lets the user fix a
// description or an assignee in place, and quick-adds one the agent missed.
// Filing happens in the review view after the meeting ends — this panel is for
// keeping the list honest while the meeting is still running.

import { useEffect, useRef, useState, type InputHTMLAttributes } from 'react'
import { Plus, Trash2, X } from 'lucide-react'

import { i18nT } from '../../../i18n/t'
import SimpleSelect from '../../../components/SimpleSelect'
import { Badge, Btn, Input, SendBtn } from '../../../components/ui'
import { PRIORITY_LABEL_KEY, type Task, type TaskPriority } from '../api'
import { useImeGuard } from '../../../hooks/useImeGuard'

const PRIORITIES: TaskPriority[] = ['high', 'medium', 'low']

function priorityBadge(priority: TaskPriority): 'err' | 'warn' | 'muted' {
  if (priority === 'high') return 'err'
  if (priority === 'medium') return 'warn'
  return 'muted'
}

type ServerSyncedInputProps = Omit<
  InputHTMLAttributes<HTMLInputElement>,
  'value' | 'defaultValue' | 'onChange' | 'onFocus' | 'onBlur'
> & {
  /** The server's copy of the field, refreshed by the meeting's poll. */
  value: string
  /** Called on blur with what the user typed, only when they changed it. */
  onCommit: (typed: string) => void
}

/**
 * A field the extractor agent may revise while the user edits it.
 *
 * While the field is not focused it shows the server's copy, so an agent
 * revision appears on the next poll. While it is focused, a revision does not
 * replace what the user is typing. On blur the user's text is written only if
 * they changed it; a field they only focused takes the server's copy, so it
 * cannot write a stale value back over the agent's update.
 */
function ServerSyncedInput({ value, onCommit, ...rest }: ServerSyncedInputProps) {
  const [draft, setDraft] = useState(value)
  const focusedRef = useRef(false)
  const atFocusRef = useRef(value)

  useEffect(() => {
    if (!focusedRef.current) setDraft(value)
  }, [value])

  return (
    <Input
      {...rest}
      value={draft}
      onChange={e => setDraft(e.target.value)}
      onFocus={e => {
        focusedRef.current = true
        atFocusRef.current = e.target.value
      }}
      onBlur={e => {
        focusedRef.current = false
        const typed = e.target.value
        if (typed === atFocusRef.current) {
          setDraft(value)
          return
        }
        onCommit(typed)
      }}
    />
  )
}

interface Props {
  tasks: Task[]
  onClose: () => void
  onAdd: (description: string) => void
  onUpdate: (taskId: string, fields: Partial<Task>) => void
  onDelete: (taskId: string) => void
}

export default function TaskSidebar({ tasks, onClose, onAdd, onUpdate, onDelete }: Props) {
  const ime = useImeGuard()
  const quickAddRef = useRef<HTMLInputElement>(null)

  const quickAdd = () => {
    const description = quickAddRef.current?.value.trim()
    if (!description) return
    onAdd(description)
    if (quickAddRef.current) quickAddRef.current.value = ''
  }

  const open = tasks.filter(task => task.review_status !== 'archived')

  return (
    // Full width below `lg`, stacked under the meeting with a bounded height --
    // the same shape MeetingWorkspace's transcript pane uses. The divider turns
    // with the layout: a left border between two stacked blocks draws a line down
    // the side of the sidebar instead of between the two.
    <aside
      className="flex-none w-full h-[42%] min-h-[260px] border-t border-border lg:h-full lg:w-[340px] lg:border-t-0 lg:border-l bg-bg flex flex-col overflow-hidden"
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
              <ServerSyncedInput
                value={task.description}
                aria-label={i18nT('apps.meetings.taskSidebar.descriptionLabel')}
                onCommit={typed => {
                  const next = typed.trim()
                  if (next && next !== task.description) onUpdate(task.id, { description: next })
                }}
              />
              <div className="flex items-center gap-2">
                <ServerSyncedInput
                  value={task.assignee}
                  placeholder={i18nT('apps.meetings.taskSidebar.unassigned')}
                  aria-label={i18nT('apps.meetings.taskSidebar.assigneeLabel')}
                  className="flex-1"
                  onCommit={typed => {
                    if (typed.trim() !== task.assignee) {
                      onUpdate(task.id, { assignee: typed.trim() })
                    }
                  }}
                />
                <SimpleSelect
                  options={PRIORITIES}
                  optionLabels={PRIORITIES.map(priority =>
                    i18nT(PRIORITY_LABEL_KEY[priority]),
                  )}
                  value={task.priority}
                  aria-label={i18nT('apps.meetings.taskSidebar.priorityLabel')}
                  onChange={value => onUpdate(task.id, { priority: value as TaskPriority })}
                  // The old select carried `flexShrink: 0` from the shared wrapper:
                  // the description Input beside it is `flex-1`, so the priority
                  // picker must keep its own width instead of being squeezed.
                  style={{ flex: '0 0 auto', minWidth: 110 }}
                />
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
          data-unsent
          type="text"
          className="flex-1"
          placeholder={i18nT('apps.meetings.taskSidebar.quickAddPlaceholder')}
          aria-label={i18nT('apps.meetings.taskSidebar.quickAddPlaceholder')}
          {...ime.bindEnter({ onEnter: quickAdd })}
        />
        <SendBtn onClick={quickAdd} aria-label={i18nT('apps.meetings.taskSidebar.add')}>
          <Plus className="lucide-inline" />
          {i18nT('apps.meetings.taskSidebar.add')}
        </SendBtn>
      </div>
    </aside>
  )
}
