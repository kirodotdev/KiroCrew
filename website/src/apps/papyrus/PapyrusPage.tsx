/**
 * PapyrusPage — the Papyrus builtin app (route `/papyrus`).
 *
 * Two views behind one route:
 *
 * - **No paper open** → `ProjectList`, which follows the standard page layout
 *   (`PageHeader` + `px-6 pb-8` container + `StatCard` row + `Card` sections).
 * - **A paper open** → a split-pane workspace: file tree, Monaco source pane and
 *   diagnostics on the left; the rendered PDF on the right; an optional co-author
 *   chat panel beyond that. A paper and its PDF need the full viewport, so the
 *   editor is deliberately full-bleed and carries its own toolbar.
 *
 * All server state is React Query (`use-react-query`); the ONLY local state is the
 * editor buffer and which pane is showing, because a buffer is genuinely local
 * until saved.
 *
 * Save-and-compile is one action, bound to Cmd/Ctrl+S: the compiler reads the file
 * off disk, so compiling an unsaved buffer would silently typeset the previous
 * revision. That is why `saveAndCompile` awaits the save before it compiles.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle, ArrowDownToLine, ArrowLeft, ArrowUpFromLine, FileDown, Loader2,
  MessageSquare, Play, Sparkles, TerminalSquare, X,
} from 'lucide-react'
import { AnimatePresence, motion } from 'framer-motion'
import { Btn, Select } from '../../components/ui'
import { useAppDispatch, useAppSelector } from '../../store'
import { addSlotOptimistic, fetchSlots } from '../../store/dashboardSlice'
import { selectComposerBusy } from '../../store/chatSlice'
import { api } from '../../api/client'
import type { ChatSlot } from '../../types'
import { papyrusApi, pdfUrl, type Diagnostic } from './api'
import { companionContextLines, DEFAULT_MAIN_FILE } from './companionPrompt'
import {
  countDiagnostics, countWords, gitBranchLabel, loadLastProject, loadSlot,
  saveLastProject, saveSlot, texFiles,
} from './lib'
import ProjectList from './ProjectList'
import FileTree from './FileTree'
import PapyrusEditor, { type PapyrusEditorHandle } from './PapyrusEditor'
import PdfPreview from './PdfPreview'
import DiagnosticsList from './DiagnosticsList'
import CoAuthorPanel from './CoAuthorPanel'

import { i18nT } from '../../i18n/t'

/** Width of the source column, as a percentage of the workspace. */
const SOURCE_PANE_PERCENT = 50

/** Width of the co-author panel when open. */
const CHAT_PANEL_WIDTH = 420

/**
 * Rejection reason when a mutation aborts because the buffer could not be saved.
 *
 * Deliberately NOT a catalog key: `saveMutation.onError` has already put the real
 * write failure on screen, so this value only unwinds the mutation and is never
 * rendered. Adding a string to 12 catalogs for text no user reads would be worse
 * than a sentinel.
 */
const FLUSH_FAILED = 'papyrus: buffer flush failed'

/** True for the flush-abort sentinel above, so a mutation that bailed on an
 *  unsaveable buffer does not overwrite the real write error with it. */
const isFlushAbort = (err: Error): boolean => err.message === FLUSH_FAILED

/** DOM id linking the toolbar's main-document label to its select. */
const MAIN_DOC_SELECT_ID = 'papyrus-main-document'

/**
 * Instructions handed to the co-author AGENT, not shown to the user.
 *
 * Deliberately not catalog keys: this is prompt text the model reads, and the
 * skill name and file-path semantics it references are English identifiers.
 * Translating it would degrade the model's instruction-following without
 * changing anything the user sees. Module-level so the i18n lint reads them as
 * constants rather than inline copy.
 */

export default function PapyrusPage() {
  const queryClient = useQueryClient()
  const dispatch = useAppDispatch()
  const navigate = useNavigate()

  const [project, setProject] = useState<string | null>(loadLastProject)
  const [currentFile, setCurrentFile] = useState('')
  const [buffer, setBuffer] = useState('')
  const [dirty, setDirty] = useState(false)
  const [diagnostics, setDiagnostics] = useState<Diagnostic[]>([])
  const [compileLog, setCompileLog] = useState('')
  const [showDiagnostics, setShowDiagnostics] = useState(false)
  const [pdfVersion, setPdfVersion] = useState(0)
  const [hasPdf, setHasPdf] = useState(false)
  const [compileMs, setCompileMs] = useState<number | null>(null)
  const [cursor, setCursor] = useState({ line: 1, column: 1 })
  const [chatOpen, setChatOpen] = useState(false)
  const [slotKey, setSlotKey] = useState<string | null>(null)
  const [slotCreating, setSlotCreating] = useState(false)
  const [error, setError] = useState('')

  const editorRef = useRef<PapyrusEditorHandle>(null)
  // The buffer's file, mirrored in a ref: the save mutation must write the file
  // the buffer BELONGS to, not whichever file a later render has selected.
  const bufferFileRef = useRef('')
  // `dirty`, mirrored in a ref. `flushBuffer` is reached from inside async
  // callbacks that already captured a previous render's `dirty` (pull's onSuccess
  // calls reloadOpenFile, which flushes again), and a state update is invisible to
  // a closure already in flight — so the flag has to be readable and writable
  // synchronously or a flush repeats and rewrites what it just saved.
  const dirtyRef = useRef(false)
  // The buffer, mirrored in a ref. `flushBuffer` has to compare the post-await
  // buffer against the snapshot it wrote, and a `buffer` read from the callback's
  // closure is frozen at render time — it can never show typing that happened
  // DURING the save, which is exactly what has to be detected.
  const bufferRef = useRef('')
  // Re-entry guard for save-and-compile. In a ref so the Cmd+S handler passed to
  // Monaco keeps a stable identity across compile cycles.
  const compilingRef = useRef(false)

  useEffect(() => { bufferFileRef.current = currentFile }, [currentFile])
  useEffect(() => { dirtyRef.current = dirty }, [dirty])
  useEffect(() => { bufferRef.current = buffer }, [buffer])
  useEffect(() => { saveLastProject(project) }, [project])

  // ── Project metadata ──────────────────────────────────────────────────────

  const projectQuery = useQuery({
    queryKey: ['papyrus', 'project', project],
    queryFn: () => papyrusApi.getProject(project as string),
    enabled: !!project,
    retry: false,
  })
  const detail = projectQuery.data
  const mainFile = detail?.main_file ?? ''
  const files = useMemo(() => detail?.files ?? [], [detail])

  // A project that cannot be opened (deleted in another tab, no .tex left) must
  // not leave the workspace mounted against nothing.
  useEffect(() => {
    if (projectQuery.isError) {
      setError(projectQuery.error instanceof Error ? projectQuery.error.message : String(projectQuery.error))
      setProject(null)
    }
  }, [projectQuery.isError, projectQuery.error])

  useEffect(() => {
    if (detail) setHasPdf(detail.has_pdf)
  }, [detail])

  // ── File loading ──────────────────────────────────────────────────────────

  const fileQuery = useQuery({
    queryKey: ['papyrus', 'file', project, currentFile],
    queryFn: () => papyrusApi.readFile(project as string, currentFile),
    enabled: !!project && !!currentFile,
    // A document is only re-read when the app asks (open, agent edit, pull), never
    // on a window refocus — a background refetch would discard unsaved typing.
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    retry: false,
  })

  // Adopt fetched content into the buffer. Guarded on `dirty` so a refetch that
  // lands while the user is mid-sentence cannot overwrite their edit.
  useEffect(() => {
    if (fileQuery.data && fileQuery.data.path === currentFile && !dirty) {
      setBuffer(fileQuery.data.content)
    }
    // `dirty` is read as a guard, not tracked: re-running when it flips to false
    // (i.e. right after a save) would re-adopt the cached pre-save content.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fileQuery.data, currentFile])

  // Open the main document when the project resolves (or changes).
  useEffect(() => {
    if (mainFile && !currentFile) {
      setCurrentFile(mainFile)
      setDirty(false)
    }
  }, [mainFile, currentFile])

  // ── Mutations ─────────────────────────────────────────────────────────────

  const saveMutation = useMutation({
    mutationFn: (payload: { path: string; content: string }) =>
      papyrusApi.saveFile(project as string, payload.path, payload.content),
    // Write what we just persisted back into the cache. Without this the entry
    // keeps the PRE-save content forever (the query is `staleTime: Infinity`
    // and is never invalidated here), so reopening the file re-adopts the old
    // text via the effect above — and the next save then writes that stale
    // buffer over the real file, silently destroying the edit in between.
    onSuccess: (_data, vars) => {
      queryClient.setQueryData(['papyrus', 'file', project, vars.path], {
        path: vars.path,
        content: vars.content,
      })
    },
    onError: (err: Error) => setError(err.message),
  })

  const invalidateFiles = useCallback(
    () => queryClient.invalidateQueries({ queryKey: ['papyrus', 'project', project] }),
    [queryClient, project],
  )

  /** Re-read the open file from disk. Used after the agent edits the paper and
   *  after a git pull rewrites it.
   *
   *  Flushes a dirty buffer FIRST rather than clearing `dirty` and overwriting it.
   *  The passive adopt effect above already refuses to overwrite an unsaved buffer;
   *  clearing the flag here would step around that exact guard, so unsaved typing
   *  was destroyed by a background refresh the user never asked for — a pull
   *  finishing, or the co-author's turn ending. The buffer is memory-only, so that
   *  is real data loss, not a stale read.
   *
   *  The save is what makes the subsequent read safe: the user's text is on disk
   *  before the fresh copy replaces the buffer, so the worst case is a visible
   *  merge conflict in git rather than silently vanished work. A failed flush
   *  ABORTS the reload — keeping the edit on screen beats replacing it with disk
   *  content the user never saw. */
  /**
   * Persist the buffer if it is dirty. Returns false when the write FAILED.
   *
   * The single guard every "something is about to replace or leave this buffer"
   * path goes through — leaving the workspace, switching files, creating a file,
   * pulling, and the post-agent refresh. The buffer is memory-only, so anything
   * that resets or overwrites it without coming through here destroys the user's
   * text outright rather than merely showing something stale. Callers must treat
   * `false` as "do not proceed": continuing past a failed flush discards exactly
   * the work the flush exists to protect.
   */
  const flushBuffer = useCallback(async (): Promise<boolean> => {
    if (!dirtyRef.current || !bufferFileRef.current) return true
    // Snapshot exactly what is being written, so the outcome can be judged against
    // it rather than against whatever the buffer holds when the request returns.
    const written = bufferRef.current
    const writtenTo = bufferFileRef.current
    try {
      await saveMutation.mutateAsync({ path: writtenTo, content: written })
    } catch {
      return false
    }
    // The user can type DURING the save. Clearing `dirty` unconditionally here
    // declared those newer keystrokes saved when only the snapshot was — and the
    // caller then switched file or left, discarding them. So the flag is cleared
    // only when the buffer still matches what actually reached disk; otherwise it
    // stays dirty and the flush reports failure, which every caller already treats
    // as "do not proceed". The user sees their text still on screen and unsaved,
    // which is recoverable; silently dropping it is not.
    if (bufferFileRef.current !== writtenTo || bufferRef.current !== written) return false
    // Clearing `dirty` HERE, not in each caller, is what makes the flush
    // idempotent — and that matters most for `pullMutation`, which flushes before
    // the rebase and then calls `reloadOpenFile` (another flush) in `onSuccess`.
    // With the flag still set, that second flush wrote the now-STALE pre-pull
    // buffer straight over the merged file: a clean disjoint merge silently lost
    // upstream's side.
    //
    // Ref first: a caller that flushes again inside the same async chain must see
    // the cleared flag immediately, not after the next render.
    dirtyRef.current = false
    setDirty(false)
    return true
  }, [saveMutation])

  const reloadOpenFile = useCallback(async () => {
    if (!project || !currentFile) return
    if (!(await flushBuffer())) return
    setDirty(false)
    const fresh = await queryClient.fetchQuery({
      queryKey: ['papyrus', 'file', project, currentFile],
      queryFn: () => papyrusApi.readFile(project, currentFile),
    })
    setBuffer(fresh.content)
  }, [project, currentFile, flushBuffer, queryClient])

  const applyCompileResult = useCallback((result: Awaited<ReturnType<typeof papyrusApi.compile>>) => {
    setDiagnostics(Array.isArray(result.errors) ? result.errors : [])
    setCompileLog(result.log || '')
    setCompileMs(result.duration_ms || null)
    if (result.ok) {
      setHasPdf(true)
      setPdfVersion(v => v + 1)
      setShowDiagnostics(false)
    } else {
      setShowDiagnostics(true)
    }
  }, [])

  const saveAndCompile = useCallback(async () => {
    if (!project || compilingRef.current) return
    compilingRef.current = true
    setCompiling(true)
    try {
      // Through `flushBuffer`, not a direct save: the compiler reads the file off
      // disk, so a flush that raced with typing must ABORT the compile rather than
      // typeset a revision the user has already moved past — and the inline version
      // also cleared `dirty` unconditionally, discarding those keystrokes.
      if (!(await flushBuffer())) return
      applyCompileResult(await papyrusApi.compile(project))
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      compilingRef.current = false
      setCompiling(false)
    }
  }, [project, flushBuffer, applyCompileResult])

  // `compiling` is a plain state flag rather than the mutation's isPending because
  // save-and-compile is two requests presented to the user as one action.
  const [compiling, setCompiling] = useState(false)

  const openFile = useCallback(async (path: string) => {
    if (!project || path === bufferFileRef.current) return
    // Flush the outgoing buffer before switching, so an unsaved edit is not lost
    // by the act of navigating away from it.
    if (!(await flushBuffer())) return
    setDirty(false)
    setCurrentFile(path)
  }, [project, flushBuffer])

  const createFileMutation = useMutation({
    // Flush FIRST: on success this switches `currentFile` to the new file, which
    // abandons the outgoing buffer exactly the way `openFile` is careful not to.
    // A failed flush aborts the create rather than trading the user's text for a
    // new empty file.
    mutationFn: async (path: string) => {
      if (!(await flushBuffer())) throw new Error(FLUSH_FAILED)
      return papyrusApi.createFile(project as string, path)
    },
    onSuccess: async (result) => {
      await invalidateFiles()
      setDirty(false)
      setCurrentFile(result.path)
    },
    onError: (err: Error) => { if (!isFlushAbort(err)) setError(err.message) },
  })

  const deleteFileMutation = useMutation({
    mutationFn: (path: string) => papyrusApi.deleteFile(project as string, path),
    onSuccess: async (_result, path) => {
      await invalidateFiles()
      if (path === bufferFileRef.current) {
        setDirty(false)
        setCurrentFile(mainFile)
      }
    },
    onError: (err: Error) => setError(err.message),
  })

  const setMainMutation = useMutation({
    mutationFn: (path: string) => papyrusApi.setMainFile(project as string, path),
    onSuccess: () => invalidateFiles(),
    onError: (err: Error) => setError(err.message),
  })

  // ── Git ───────────────────────────────────────────────────────────────────

  const gitQuery = useQuery({
    queryKey: ['papyrus', 'git', project],
    queryFn: () => papyrusApi.gitStatus(project as string),
    enabled: !!project,
    retry: false,
  })
  const git = gitQuery.data
  const invalidateGit = useCallback(
    () => queryClient.invalidateQueries({ queryKey: ['papyrus', 'git', project] }),
    [queryClient, project],
  )

  const pullMutation = useMutation({
    // Flush BEFORE the pull, not after: a rebase rewrites the file on disk, so
    // saving afterwards would push the pre-pull buffer over upstream's version.
    // Flushing first turns the bad case into a normal git conflict the user can see.
    mutationFn: async () => {
      if (!(await flushBuffer())) throw new Error(FLUSH_FAILED)
      return papyrusApi.gitPull(project as string)
    },
    onSuccess: async () => {
      await invalidateGit()
      await invalidateFiles()
      await reloadOpenFile()
    },
    onError: (err: Error) => { if (!isFlushAbort(err)) setError(err.message) },
  })

  const pushMutation = useMutation({
    mutationFn: async () => {
      // Same guard as every other transition: committing a snapshot the user has
      // already typed past would push the wrong revision AND lose the newer text.
      if (!(await flushBuffer())) throw new Error(FLUSH_FAILED)
      await papyrusApi.gitCommit(project as string, i18nT('apps.papyrus.workspace.default_commit_message'))
      return papyrusApi.gitPush(project as string)
    },
    onSuccess: () => invalidateGit(),
    onError: (err: Error) => { if (!isFlushAbort(err)) setError(err.message) },
  })

  // ── Co-author session ─────────────────────────────────────────────────────

  // Adopt the remembered slot for this paper as soon as the project changes.
  useEffect(() => {
    setSlotKey(project ? loadSlot(project) : null)
  }, [project])

  /** Build the silent context that tells the agent which paper it is working on.
   *  The agent needs the project name and the main document; the skill supplies
   *  everything else (where projects live, how to compile, the style rules). */
  const companionContext = useCallback(() => {
    return companionContextLines(project ?? '', mainFile).join('\n')
  }, [project, mainFile])

  const startSession = useCallback(async () => {
    if (!project || slotCreating) return
    setSlotCreating(true)
    try {
      // No `name`: the backend mints a unique slot key. Reusing a name-derived key
      // would append onto an archived session's history file.
      const created = await api.createChatSlot(
        undefined, undefined, undefined, undefined, undefined,
        i18nT('apps.papyrus.workspace.session_title', { name: project }),
      )
      const key = created.key as string
      dispatch(addSlotOptimistic({
        key,
        title: created.title || project,
        messages: 0,
        running: false,
      } as ChatSlot))
      api.chatSlotContext(key, companionContext(), {
        source: 'papyrus-co-author', ephemeral: true,
      }).catch(() => undefined)
      dispatch(fetchSlots())
      saveSlot(project, key)
      setSlotKey(key)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setSlotCreating(false)
    }
  }, [project, slotCreating, dispatch, companionContext])

  const toggleChat = useCallback(() => {
    setChatOpen(open => {
      if (!open && !slotKey) void startSession()
      return !open
    })
  }, [slotKey, startSession])

  // When the co-author finishes a turn, re-read the open file and recompile: the
  // agent edits the paper on disk, so the pane the user is watching is stale until
  // this runs. Keyed on the busy->idle transition rather than on a `chat_done`
  // websocket subscription of its own, because `selectComposerBusy` is the store's
  // single answer to "is this session working" and already merges every signal
  // that decides it (stream state, sub-agents, the slots snapshot).
  const coAuthorBusy = useAppSelector(state => selectComposerBusy(state, slotKey))
  const prevBusyRef = useRef(false)
  useEffect(() => {
    const wasBusy = prevBusyRef.current
    prevBusyRef.current = coAuthorBusy
    if (!wasBusy || coAuthorBusy || !slotKey) return
    void (async () => {
      try {
        await invalidateFiles()
        await reloadOpenFile()
        if (project) applyCompileResult(await papyrusApi.compile(project))
      } catch {
        // A refresh failure is not worth a banner: the user's next Cmd+S recovers,
        // and surfacing it would blame them for the agent's turn.
      }
    })()
  }, [coAuthorBusy, slotKey, project, invalidateFiles, reloadOpenFile, applyCompileResult])

  // ── Derived ───────────────────────────────────────────────────────────────

  const counts = useMemo(() => countDiagnostics(diagnostics), [diagnostics])
  const wordCount = useMemo(() => countWords(buffer), [buffer])
  const branchLabel = gitBranchLabel(git)
  const pdfSrc = project && hasPdf ? pdfUrl(project, pdfVersion) : null
  const mainCandidates = useMemo(() => texFiles(files), [files])

  const closeProject = useCallback(async () => {
    // Flush the outgoing buffer FIRST, for the same reason `openFile` does: leaving
    // the workspace is navigating away from an unsaved edit, and the buffer lives
    // only in memory, so resetting it without a save destroys the work outright
    // rather than leaving it recoverable on disk. The toolbar advertises
    // "Editing {file} — unsaved" right next to this button, which makes a silent
    // discard especially unexpected.
    //
    // Save-then-close rather than a confirm dialog: it matches the flush `openFile`
    // already performs, so both ways of leaving a file behave identically, and it
    // never asks the user a question whose safe answer is always "save".
    // Do NOT tear down the workspace if the flush failed — that would discard the
    // very edits this guard exists to protect. `saveMutation.onError` has already
    // surfaced the message, so staying put is enough.
    if (project && !(await flushBuffer())) return
    setProject(null)
    setCurrentFile('')
    setBuffer('')
    setDirty(false)
    setDiagnostics([])
    setCompileLog('')
    setHasPdf(false)
    setCompileMs(null)
    setChatOpen(false)
  }, [project, flushBuffer])

  const openProject = useCallback((name: string) => {
    setError('')
    setProject(name)
    setCurrentFile('')
    setBuffer('')
    setDirty(false)
    setDiagnostics([])
    setCompileLog('')
    setCompileMs(null)
  }, [])

  const onCreateFileClick = useCallback(() => {
    // A one-field prompt is the right weight for "name a new file"; a modal would
    // be more chrome than the action deserves.
    const name = window.prompt(i18nT('apps.papyrus.workspace.new_file_prompt'))
    const trimmed = name?.trim()
    if (trimmed) createFileMutation.mutate(trimmed)
  }, [createFileMutation])

  const onDeleteFileClick = useCallback((path: string) => {
    if (window.confirm(i18nT('apps.papyrus.workspace.delete_file_confirm', { file: path }))) {
      deleteFileMutation.mutate(path)
    }
  }, [deleteFileMutation])

  if (!project) {
    return (
      <>
        {error && (
          <div className="mx-6 mt-2 bg-danger/10 border border-danger/20 rounded-lg p-3 flex items-start gap-3 animate-rise" role="alert">
            <AlertTriangle className="lucide-inline text-danger shrink-0 mt-0.5" />
            <div className="flex-1 text-[13px] text-text break-words">{error}</div>
            <button
              type="button"
              onClick={() => setError('')}
              aria-label={i18nT('apps.papyrus.page.dismiss_error')}
              className="p-1 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer bg-transparent border-none transition-colors"
            >
              <X className="lucide-inline" />
            </button>
          </div>
        )}
        <ProjectList onOpenProject={openProject} />
      </>
    )
  }

  return (
    <div className="flex flex-col flex-1 min-h-0" data-testid="papyrus-workspace">
      {/* Toolbar */}
      <div className="flex flex-wrap items-center gap-2 px-3 py-2 border-b border-border bg-card shrink-0">
        <Btn onClick={closeProject}>
          <ArrowLeft className="lucide-inline" />
          {i18nT('apps.papyrus.workspace.papers')}
        </Btn>
        <span className="text-[13px] font-medium text-text-strong truncate max-w-[12rem]">{project}</span>

        {/* Nested AND explicitly associated (`htmlFor`/`id`), which is what
            actually reaches assistive technology through the shared `Select`.
            `jsx-a11y/label-has-for` still fires because `Select` is a
            `forwardRef` component, so the rule cannot see the `<select>` it
            wraps and cannot verify the nesting half — the association is real,
            the lint is not. Same false positive as the other `Select`-in-label
            sites in this repo. */}
        {/* eslint-disable-next-line jsx-a11y/label-has-for */}
        <label
          htmlFor={MAIN_DOC_SELECT_ID}
          className="flex items-center gap-1.5 text-[12px] text-muted"
        >
          {i18nT('apps.papyrus.workspace.main_document')}
          <Select
            id={MAIN_DOC_SELECT_ID}
            value={mainFile}
            onChange={e => setMainMutation.mutate(e.target.value)}
            disabled={setMainMutation.isPending || mainCandidates.length === 0}
          >
            {mainCandidates.map(file => (
              <option key={file} value={file}>{file}</option>
            ))}
          </Select>
        </label>

        <span className="text-[12px] text-muted truncate">
          {dirty
            ? i18nT('apps.papyrus.workspace.editing_unsaved', { file: currentFile })
            : i18nT('apps.papyrus.workspace.editing', { file: currentFile })}
        </span>

        <div className="flex-1" />

        <Btn primary onClick={saveAndCompile} disabled={compiling}>
          {compiling
            ? <Loader2 className="lucide-inline animate-spin motion-reduce:animate-none" />
            : <Play className="lucide-inline" />}
          {compiling
            ? i18nT('apps.papyrus.workspace.compiling')
            : i18nT('apps.papyrus.workspace.compile')}
        </Btn>

        <Btn
          onClick={() => setShowDiagnostics(v => !v)}
          aria-pressed={showDiagnostics}
        >
          <TerminalSquare className="lucide-inline" />
          {i18nT('apps.papyrus.workspace.log')}
          {counts.errors > 0 && (
            <span className="ml-1 text-danger">{counts.errors}</span>
          )}
        </Btn>

        {pdfSrc && (
          <a
            href={pdfSrc}
            download={`${project}.pdf`}
            className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md border border-border text-[13px] text-muted hover:text-text hover:border-border-strong hover:bg-bg-hover no-underline transition-all focus-ring"
          >
            <FileDown className="lucide-inline" />
            {i18nT('apps.papyrus.workspace.pdf')}
          </a>
        )}

        {git?.is_git && (
          <>
            <span className="font-mono text-[12px] text-muted" title={i18nT('apps.papyrus.workspace.git_branch')}>
              {branchLabel}
              {!!git.ahead && ` +${git.ahead}`}
              {!!git.behind && ` -${git.behind}`}
            </span>
            {git.has_remote && (
              <Btn onClick={() => pullMutation.mutate()} disabled={pullMutation.isPending}>
                {pullMutation.isPending
                  ? <Loader2 className="lucide-inline animate-spin motion-reduce:animate-none" />
                  : <ArrowDownToLine className="lucide-inline" />}
                {i18nT('apps.papyrus.workspace.pull')}
              </Btn>
            )}
            <Btn onClick={() => pushMutation.mutate()} disabled={pushMutation.isPending}>
              {pushMutation.isPending
                ? <Loader2 className="lucide-inline animate-spin motion-reduce:animate-none" />
                : <ArrowUpFromLine className="lucide-inline" />}
              {i18nT('apps.papyrus.workspace.push')}
            </Btn>
          </>
        )}

        <Btn onClick={toggleChat} aria-pressed={chatOpen}>
          {chatOpen ? <MessageSquare className="lucide-inline" /> : <Sparkles className="lucide-inline" />}
          {i18nT('apps.papyrus.workspace.co_author')}
        </Btn>
      </div>

      {error && (
        <div className="mx-3 mt-2 bg-danger/10 border border-danger/20 rounded-lg p-2.5 flex items-start gap-3 animate-rise" role="alert">
          <AlertTriangle className="lucide-inline text-danger shrink-0 mt-0.5" />
          <div className="flex-1 text-[13px] text-text break-words">{error}</div>
          <button
            type="button"
            onClick={() => setError('')}
            aria-label={i18nT('apps.papyrus.page.dismiss_error')}
            className="p-1 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer bg-transparent border-none transition-colors"
          >
            <X className="lucide-inline" />
          </button>
        </div>
      )}

      {/* Workspace */}
      <div className="flex flex-1 min-h-0">
        {/* Source column: file tree + editor + status bar (+ diagnostics) */}
        <div
          className="flex flex-col min-h-0 min-w-0"
          style={{ width: `${SOURCE_PANE_PERCENT}%` }}
        >
          <div className="flex flex-1 min-h-0">
            <div className="w-44 shrink-0 min-h-0">
              <FileTree
                files={files}
                currentFile={currentFile}
                mainFile={mainFile}
                onOpenFile={openFile}
                onCreateFile={onCreateFileClick}
                onDeleteFile={onDeleteFileClick}
              />
            </div>
            <div className="flex-1 min-w-0 min-h-0">
              <PapyrusEditor
                ref={editorRef}
                path={currentFile || mainFile || DEFAULT_MAIN_FILE}
                value={buffer}
                onChange={value => {
                  setBuffer(value)
                  bufferRef.current = value
                  dirtyRef.current = true
                  setDirty(true)
                }}
                onSave={saveAndCompile}
                diagnostics={currentFile === mainFile ? diagnostics : []}
                onCursorChange={(line, column) => setCursor({ line, column })}
              />
            </div>
          </div>

          {/* Status bar */}
          <div className="flex items-center gap-4 px-3 py-1 border-t border-border bg-bg-subtle text-[12px] text-muted shrink-0">
            <span title={i18nT('apps.papyrus.workspace.save_and_compile_hint')}>
              {i18nT('apps.papyrus.workspace.cursor_position', { line: cursor.line, column: cursor.column })}
            </span>
            <span>{i18nT('apps.papyrus.workspace.word_count', { count: wordCount })}</span>
            {compileMs !== null && (
              <span>{i18nT('apps.papyrus.workspace.compile_duration', { ms: compileMs })}</span>
            )}
            {counts.errors > 0 && (
              <span className="text-danger">
                {i18nT('apps.papyrus.workspace.error_count', { count: counts.errors })}
              </span>
            )}
            {counts.warnings > 0 && (
              <span className="text-warn">
                {i18nT('apps.papyrus.workspace.warning_count', { count: counts.warnings })}
              </span>
            )}
          </div>

          <AnimatePresence initial={false}>
            {showDiagnostics && (
              <motion.div
                key="diagnostics"
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: 'auto', opacity: 1 }}
                exit={{ height: 0, opacity: 0 }}
                transition={{ duration: 0.16 }}
                className="border-t border-border bg-card overflow-hidden shrink-0 max-h-56"
              >
                <DiagnosticsList
                  diagnostics={diagnostics}
                  log={compileLog}
                  onJumpToLine={line => editorRef.current?.jumpToLine(line)}
                />
              </motion.div>
            )}
          </AnimatePresence>
        </div>

        {/* PDF column */}
        <div className="flex flex-col flex-1 min-w-0 min-h-0 border-l border-border">
          <PdfPreview src={pdfSrc} downloadName={`${project}.pdf`} />
        </div>

        {/* Co-author column */}
        <AnimatePresence initial={false}>
          {chatOpen && (
            <motion.div
              key="co-author"
              initial={{ width: 0, opacity: 0 }}
              animate={{ width: CHAT_PANEL_WIDTH, opacity: 1 }}
              exit={{ width: 0, opacity: 0 }}
              transition={{ duration: 0.18 }}
              className="shrink-0 min-h-0 overflow-hidden"
            >
              <div style={{ width: CHAT_PANEL_WIDTH }} className="h-full min-h-0">
                <CoAuthorPanel
                  slotKey={slotKey}
                  creating={slotCreating}
                  onStartSession={startSession}
                  onOpenFull={() => navigate(`/chat?sid=${encodeURIComponent(slotKey || '')}`)}
                  onClose={() => setChatOpen(false)}
                />
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </div>
  )
}
