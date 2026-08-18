/** KanbanPage — the builtin app entry point for the Kanban task board. */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { KanbanSquare, Plus, Search, X } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import * as api from './api'
import { Board } from './components/Board'
import { CreateTaskForm } from './components/CreateTaskForm'
import { TaskDetail } from './components/TaskDetail'
import type { TaskRecord, TaskStatus } from './types'
import { i18nT } from '../../i18n/t'

const QUERY_KEY = ['kanban', 'tasks']

export default function KanbanPage() {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [search, setSearch] = useState('')
  // The id, not the record. Holding the object froze the modal at open time, so
  // a background settle or the namer landing a title never reached it; keying by
  // id means the modal reads whatever the board's poll last returned, and a card
  // deleted elsewhere closes it instead of showing a ghost.
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [showCreateForm, setShowCreateForm] = useState(false)
  // Survives the form's own unmount so a failed create can hand the prompt back.
  const [draftPrompt, setDraftPrompt] = useState('')

  // ── Data ──
  const { data, isLoading } = useQuery({
    queryKey: QUERY_KEY,
    queryFn: () => api.fetchTasks(),
    // Running tasks settle asynchronously and a freshly created card is still
    // being named, so poll faster while either is outstanding and back off to
    // the idle cadence once the board is quiet.
    refetchInterval: q => {
      const rows = q.state.data?.tasks ?? []
      return rows.some(t => t.refining || t.status === 'running') ? 1500 : 5000
    },
  })

  const tasks = data?.tasks ?? []
  const selectedTask = tasks.find((t: TaskRecord) => t.id === selectedId) ?? null
  const needle = search.trim().toLowerCase()
  const filteredTasks = needle
    ? tasks.filter(t =>
        t.title.toLowerCase().includes(needle) ||
        t.description.toLowerCase().includes(needle) ||
        t.prompt.toLowerCase().includes(needle) ||
        t.tags.some((tag: string) => tag.toLowerCase().includes(needle))
      )
    : tasks

  // ── Mutations ──
  const invalidate = () => queryClient.invalidateQueries({ queryKey: QUERY_KEY })

  // Every mutation can now fail in a way the user must see rather than infer
  // from nothing happening: PATCH answers 400 for a blank or non-string title,
  // and POST /run answers 409 when the card is already running. Without a
  // surface for that the board silently discards the refusal.
  const [actionError, setActionError] = useState<string | null>(null)
  const onError = (err: unknown) =>
    setActionError(err instanceof Error ? err.message : String(err))

  const moveMutation = useMutation({
    mutationFn: ({ id, status }: { id: string; status: TaskStatus }) => api.moveTask(id, status),
    onSuccess: invalidate,
    onError,
  })

  const createMutation = useMutation({
    mutationFn: api.createTask,
    onSuccess: () => {
      // The form is already dismissed by its handler; drop the draft it held so
      // the next open starts blank.
      setDraftPrompt('')
      invalidate()
    },
    onError: (err, variables) => {
      // Bring the dismissed form back with the prompt still in it. The banner
      // alone would tell the user it failed while their typing was already gone.
      // `prompt` is optional on the endpoint (a card can be created from a title),
      // and only the prompt form is dismissed on submit, so there is nothing to
      // restore without one.
      if (variables.prompt) {
        setDraftPrompt(variables.prompt)
        setShowCreateForm(true)
      }
      onError(err)
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: Partial<TaskRecord> }) => api.updateTask(id, patch),
    // No setSelectedTask here: the modal reads the live record out of the board
    // query, so invalidating is what refreshes it.
    onSuccess: invalidate,
    onError,
  })

  const deleteMutation = useMutation({
    mutationFn: api.deleteTask,
    onSuccess: () => { invalidate(); setSelectedId(null) },
    onError,
  })

  const runMutation = useMutation({
    mutationFn: (task: TaskRecord) => api.runTask(task.id),
    onSuccess: invalidate,
    onError,
  })

  // Reconciling cards left in Running by a gateway restart is server state like
  // any other write, so it goes through the mutation lifecycle rather than a
  // hand-rolled `.then()` in an effect: React Query owns the invalidation, and
  // its status is inspectable instead of living in a closure.
  //
  // Deliberately no `onError`: a failed reconcile is not the user's problem to
  // act on. The board keeps polling and a genuinely stuck card is still visible,
  // so raising the action banner here would report a background repair the user
  // never asked for.
  const reconcileMutation = useMutation({
    mutationFn: api.reconcileTasks,
    onSuccess: invalidate,
  })

  // Once per mount, and once even under StrictMode's double-invoke: reconcile is
  // idempotent, but firing it twice would settle the same cards twice and log a
  // second sweep for no gain.
  const reconcileFiredRef = useRef(false)
  useEffect(() => {
    if (reconcileFiredRef.current) return
    reconcileFiredRef.current = true
    reconcileMutation.mutate()
  }, [reconcileMutation])

  // ── Handlers ──
  const handleMove = useCallback((taskId: string, newStatus: TaskStatus) => {
    moveMutation.mutate({ id: taskId, status: newStatus })
  }, [moveMutation])

  const handleTaskClick = useCallback((task: TaskRecord) => setSelectedId(task.id), [])

  const handleTaskRun = useCallback((task: TaskRecord) => {
    runMutation.mutate(task)
  }, [runMutation])

  const handleCreateFromPrompt = useCallback((prompt: string) => {
    // Dismiss first, then dispatch. Creating a card does not wait on a model --
    // the backend returns a provisional title and names it a few seconds later
    // -- so holding the form open until the POST returns would leave the user
    // staring at a filled-in form with nothing happening, and invite a second
    // submit. Dismissing does not risk the text: a failed create re-opens the
    // form with the prompt restored, alongside the page's error banner.
    setShowCreateForm(false)
    createMutation.mutate({ prompt, status: 'todo' })
  }, [createMutation])

  /**
   * Open the chat session that ran a task. The dashboard addresses a session
   * by its slot key in the `sid` query param — the path slug is cosmetic.
   */
  const handleOpenSession = useCallback((sessionKey: string) => {
    navigate(`/chat?sid=${encodeURIComponent(sessionKey)}`)
  }, [navigate])

  // ── Render ──
  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Header — title and controls both sit left, so the eye travels once */}
      <div className="px-4 pt-4 pb-3">
        <div className="flex items-center gap-4 flex-wrap">
          <div className="flex items-baseline gap-2">
            <h1 className="text-xl font-semibold text-text-strong">{i18nT('apps.kanban.kanbanPage.kanban')}</h1>
            <span className="text-xs text-muted">
              {i18nT('apps.kanban.kanbanPage.task_count', { count: tasks.length })}
            </span>
          </div>

          <button
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-accent text-accent-fg text-xs font-medium hover:bg-accent-hover transition-colors"
            onClick={() => setShowCreateForm(true)}
          >
            <Plus size={14} />
            {i18nT('apps.kanban.kanbanPage.new_task')}
          </button>

          <div className="relative">
            <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted" />
            <input
              className="pl-8 pr-3 py-1.5 text-xs bg-bg border border-border rounded-md text-text placeholder:text-muted focus:outline-none focus:ring-1 focus:ring-accent w-[200px]"
              placeholder={i18nT('apps.kanban.kanbanPage.search_tasks_placeholder')}
              value={search}
              onChange={e => setSearch(e.target.value)}
              aria-label={i18nT('apps.kanban.kanbanPage.search_tasks')}
            />
          </div>

          {needle && (
            <span className="text-[11px] text-muted">
              {i18nT('apps.kanban.kanbanPage.match_count', { count: filteredTasks.length })}
            </span>
          )}
        </div>
      </div>

      {/* A refused mutation says so. Dismissible rather than auto-hiding: a 409
          on Run is the answer to something the user just clicked, and a banner
          that vanishes on a timer is one they can miss entirely. */}
      {actionError && (
        <div className="mx-4 mb-3 flex items-start gap-2 px-3 py-2 rounded-md border border-danger bg-danger-subtle text-xs text-danger" role="alert">
          <span className="flex-1">{actionError}</span>
          <button
            className="text-danger hover:text-text-strong transition-colors"
            onClick={() => setActionError(null)}
            aria-label={i18nT('apps.kanban.kanbanPage.dismiss_error')}
          >
            <X size={14} />
          </button>
        </div>
      )}

      {/* Board */}
      <div className="flex-1 overflow-hidden px-4 pb-4">
        {isLoading ? (
          <div className="flex items-center justify-center h-full">
            <div className="flex items-center gap-2 text-muted">
              <KanbanSquare size={20} className="animate-pulse" />
              <span className="text-sm">{i18nT('apps.kanban.kanbanPage.loading_board')}</span>
            </div>
          </div>
        ) : (
          <Board
            tasks={filteredTasks}
            onMove={handleMove}
            onTaskClick={handleTaskClick}
            onTaskRun={handleTaskRun}
            onOpenSession={handleOpenSession}
          />
        )}
      </div>

      {/* Task detail — a centered modal */}
      {selectedTask && (
        <TaskDetail
          task={selectedTask}
          onClose={() => setSelectedId(null)}
          onUpdate={(id, patch) => updateMutation.mutate({ id, patch })}
          onMove={handleMove}
          onRun={handleTaskRun}
          onDelete={id => deleteMutation.mutate(id)}
          onOpenSession={handleOpenSession}
        />
      )}

      {/* Create form */}
      {showCreateForm && (
        <CreateTaskForm
          initialPrompt={draftPrompt}
          onSubmit={handleCreateFromPrompt}
          onCancel={() => {
            setDraftPrompt('')
            setShowCreateForm(false)
          }}
        />
      )}
    </div>
  )
}
