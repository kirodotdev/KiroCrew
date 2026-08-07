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
import { Btn } from '../../components/ui'
import SearchableSelect from '../../components/SearchableSelect'
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
import { fmtUnit } from '../../i18n/format'

/** Width of the source column, as a percentage of the workspace. */
const SOURCE_PANE_PERCENT = 50

const MS_PER_SECOND = 1000

/** Compile time, readable and localized.
 *
 * Was a catalog string `"{{ms}} ms"`, which had two defects: a bibliography build
 * rendered as `48231 ms` (arithmetic the reader should not have to do), and the
 * number skipped `Intl` entirely, so no locale got its own grouping — de expects
 * `48.231`, bn expects Bengali digits. `fmtUnit` supplies BOTH the localized
 * number and the localized unit, which is why the key is deleted rather than
 * reworded: keeping it would weld a hardcoded `ms` onto an already-localized
 * number, the exact shape `i18n/unitLiterals.test.ts` exists to catch.
 */
function compileDurationLabel(ms: number): string {
  return ms < MS_PER_SECOND
    ? fmtUnit(ms, 'millisecond', { maximumFractionDigits: 0 })
    : fmtUnit(ms / MS_PER_SECOND, 'second', { maximumFractionDigits: 1 })
}

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
  // The file whose on-disk copy diverged from an unsaved buffer (a co-author edit
  // arriving while the user was typing). Blocks saves until reconciled — see
  // `reloadOpenFile`'s no-flush branch and `resolveConflict`.
  const [conflictFile, setConflictFile] = useState<string | null>(null)

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
  // `conflictFile`, mirrored in a ref for the same reason `dirty` is: `flushBuffer`
  // is reached from inside async chains that already captured a previous render's
  // state, and a state read from such a closure cannot see a conflict recorded
  // during the same chain.
  const conflictFileRef = useRef<string | null>(null)
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
  useEffect(() => { conflictFileRef.current = conflictFile }, [conflictFile])
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
  //
  // But ONLY when the open genuinely failed — that is, when there is no cached
  // detail to keep working against. `projectQuery` refetches on window focus (it
  // does not set `refetchOnWindowFocus: false`, unlike `fileQuery` below), so a
  // BACKGROUND refetch that fails — laptop resumed, gateway restarting, wifi
  // dropped — also flips `isError`. Unmounting the workspace then destroys the
  // editor buffer, and that buffer is the ONLY copy of unsaved typing: Papyrus keeps
  // the working text in memory and writes on explicit save. So a transient network
  // blip while the user was mid-paragraph silently discarded the paragraph.
  //
  // Keyed on `projectQuery.data` rather than on `isFetching`/`fetchStatus`: what
  // matters is not why the request ran but whether we still hold something to show.
  // React Query keeps the last successful data alongside the error, so a failed
  // refetch has data and an initial failure does not — which is exactly the
  // distinction. The error text is surfaced either way, so a failed background
  // refresh is still reported; it just no longer takes the document with it.
  useEffect(() => {
    if (!projectQuery.isError) return
    setError(projectQuery.error instanceof Error ? projectQuery.error.message : String(projectQuery.error))
    if (!projectQuery.data) setProject(null)
  }, [projectQuery.isError, projectQuery.error, projectQuery.data])

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

  // True while the editor is showing text that does not belong to `currentFile`.
  //
  // `openFile` sets `currentFile` at once, but `buffer` is only replaced when
  // `fileQuery` resolves for that path — so until then the visible text is the
  // PREVIOUS file's. Keyed on the query's own resolved path rather than a
  // loading flag, because the cache can serve a different file's entry
  // synchronously and `isFetching` would already be false.
  const contentIsStale =
    Boolean(currentFile) && fileQuery.data?.path !== currentFile

  // An unresolved co-author conflict on the file currently open. Read-only for the
  // same reason `contentIsStale` is: editing on top of a buffer that cannot be saved
  // just accumulates work that will be refused.
  const hasConflict = Boolean(currentFile) && conflictFile === currentFile


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
    // An UNRESOLVED CONFLICT refuses the write. This is the chokepoint every save
    // goes through (Cmd+S, compile, pull, close, switching files), so refusing here
    // covers all of them rather than each remembering to check.
    //
    // Reported as a failure, which every caller already treats as "do not proceed" —
    // the same contract as a mid-save keystroke. Without this the divergence was only
    // postponed: the buffer was left alone by the refresh, and the next Cmd+S wrote it
    // over the agent's version silently, with no refresh to blame it on.
    if (conflictFileRef.current === bufferFileRef.current) return false
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

  /**
   * Re-read the open file from disk.
   *
   * `flushWhenDirty` decides what happens to an UNSAVED buffer, and the two callers
   * genuinely want opposite things:
   *
   * - **pull** (`true`) already flushed before the rebase, so its inner flush is a
   *   no-op that only keeps the function idempotent. Flushing is also correct there:
   *   the user's text predates the merge and belongs on disk before it.
   * - **the co-author refresh** (`false`) is the opposite case. The agent just
   *   edited this file, and the browser buffer is what is stale. Flushing first
   *   SAVED that stale buffer over the agent's changes and then read the file back —
   *   so the agent's work was destroyed by the very refresh meant to display it, and
   *   the reload then showed the overwrite as if it were the result.
   *
   * With `false`, a dirty buffer is instead left exactly as it is: the disk keeps the
   * agent's version, the editor keeps the user's typing, and the post-await guard
   * below declines to overwrite. Nothing is lost on either side, and the user's next
   * save is a deliberate act on a document they can see. The conflict surfaces as
   * "refuse to clobber" rather than a modal — consistent with how `flushBuffer`
   * already reports a mid-save keystroke.
   */
  const reloadOpenFile = useCallback(async (flushWhenDirty = true): Promise<boolean> => {
    if (!project || !currentFile) return false
    if (flushWhenDirty) {
      if (!(await flushBuffer())) return false
      setDirty(false)
    } else if (dirtyRef.current) {
      // Deliberately no flush and no adopt: the agent's version stays on disk and
      // the user's unsaved text stays on screen.
      //
      // But leaving it there is not enough on its own. Nothing had CHANGED about the
      // buffer, so the user's next Cmd+S wrote it straight over the agent's version —
      // the clobber was postponed, not prevented, and the second time it happened
      // silently with no refresh to blame. So the divergence is recorded and saves are
      // refused until it is reconciled; `conflictFile` drives the editor's read-only
      // state and the toolbar notice, the same mechanism `contentIsStale` already uses.
      setConflictFile(currentFile)
      return false
    }
    const readFrom = currentFile
    const fresh = await queryClient.fetchQuery({
      queryKey: ['papyrus', 'file', project, currentFile],
      queryFn: () => papyrusApi.readFile(project, currentFile),
      // `staleTime: 0` is load-bearing, not a default restated. `fileQuery` above
      // is `staleTime: Infinity` and `saveMutation.onSuccess` seeds this exact key
      // with the text it just wrote — so `fetchQuery` finds a FRESH entry and
      // returns the cached pre-pull content without reading the disk. Every caller
      // here (pull, agent edit, co-author refresh) is reloading precisely because
      // the file changed underneath, so a cache hit is always the wrong answer:
      // the merged file would be discarded and the next save would write the
      // pre-pull text back over upstream's side.
      staleTime: 0,
    })
    // `fetchQuery` is an await, so the editor stays live across it. Adopt the
    // fetched text ONLY if nothing changed underneath: a keystroke during the
    // round trip sets `dirtyRef`, and switching files changes `bufferFileRef` —
    // in either case this response is stale and overwriting with it would
    // silently discard what the user just typed. Same guard as `flushBuffer`'s
    // post-await check, for the same reason and in the opposite direction.
    if (bufferFileRef.current !== readFrom || dirtyRef.current) {
      // The buffer went dirty (or the file switched) DURING the fetch. For a
      // `flushWhenDirty: false` reload — the co-author refresh — that is the same
      // divergence the `dirtyRef` pre-check records, just discovered one await later:
      // the agent's version is on disk, the user's newer typing is on screen, and
      // nothing adopted. Without recording it here the guard was window-dependent —
      // typing BEFORE the fetch was protected, typing DURING it was not, and the next
      // save silently overwrote the agent.
      //
      // Only for the file we actually read, so a mid-flight file switch does not
      // mark the newly-opened document as conflicted.
      if (!flushWhenDirty && dirtyRef.current && bufferFileRef.current === readFrom) {
        conflictFileRef.current = readFrom
        setConflictFile(readFrom)
      }
      return false
    }
    setBuffer(fresh.content)
    // Adopting the disk copy IS the reconciliation, so the conflict is cleared here
    // — including the `flushWhenDirty: true` path, where the buffer was saved first.
    if (conflictFileRef.current === readFrom) {
      conflictFileRef.current = null
      setConflictFile(null)
    }
    // Returns whether the disk copy was ADOPTED, not merely whether no error was
    // thrown. `resolveConflict` needs that distinction: this function has two
    // no-adopt exits (a stale response, a buffer that went dirty mid-flight) that
    // resolve successfully, and treating them as a resolved conflict would drop the
    // guard while the editor still shows the old text.
    return true
  }, [project, currentFile, flushBuffer, queryClient])

  /** Take the agent's version, discarding the unsaved buffer.

   * The one exit from a conflict. Deliberately "theirs wins" rather than a merge UI:
   * the agent's copy is on disk and complete, the buffer is an interrupted edit, and
   * the user can re-apply their change on top of a document they can now see. A merge
   * view is a real feature — this is the escape hatch that keeps the block honest.
   */
  const resolveConflict = useCallback(async () => {
    const conflicted = conflictFileRef.current
    // Confirm FIRST, before anything is cleared, and only when there is something
    // to lose. The button used to say "Reload" — which promises a refresh — while
    // this handler deletes the user's unsaved buffer with no undo. The label now
    // says what it does, and this restates the stakes for the one action in the
    // app that destroys typing the user cannot get back.
    //
    // Placed above the clear so the early return leaves every guard exactly as it
    // was; the ordering the tests pin (clear -> reload) is unchanged.
    if (dirtyRef.current && !window.confirm(
      i18nT('apps.papyrus.workspace.co_author_conflict_discard_confirm', {
        file: conflicted ?? '',
      }),
    )) return
    // Cleared BEFORE the reload, and the refs too: `reloadOpenFile` refuses to adopt
    // while the buffer is dirty, and its no-flush branch would otherwise re-record the
    // very conflict being resolved. So the guard has to be down for the reload to run.
    conflictFileRef.current = null
    setConflictFile(null)
    dirtyRef.current = false
    setDirty(false)
    let adopted = false
    try {
      adopted = await reloadOpenFile(false)
    } catch {
      adopted = false
    }
    if (!adopted) {
      // RESTORE the guard. Clearing it before the reload is necessary, but leaving it
      // cleared when the reload FAILS is what the guard exists to prevent: the editor
      // still shows the stale buffer, and it would now be writable — so the next save
      // overwrites the co-author's version, which is the exact overwrite this whole
      // mechanism was added to stop, reached by a failed recovery instead of a
      // successful edit.
      //
      // `dirty` is restored too. Without it the buffer is "clean" and the read-only
      // state derived from `hasConflict` would be the only thing holding it, so a
      // later render that recomputes could let typing through.
      conflictFileRef.current = conflicted
      setConflictFile(conflicted)
      dirtyRef.current = true
      setDirty(true)
    }
  }, [reloadOpenFile])

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

  // Takes the commit message, because this button COMMITS before it pushes:
  // `gitCommit` runs `git add -A` server-side, so every change in the paper —
  // including files the user has not looked at — goes out under this message. A
  // canned message hid that: "Push" read as "publish what I already committed",
  // and there was no field, no preview and no way to describe the change.
  const pushMutation = useMutation({
    mutationFn: async (message: string) => {
      // Same guard as every other transition: committing a snapshot the user has
      // already typed past would push the wrong revision AND lose the newer text.
      if (!(await flushBuffer())) throw new Error(FLUSH_FAILED)
      // An empty message is deliberately forwarded as `""` rather than filled in
      // here: the backend owns the fallback (`gitops.DEFAULT_COMMIT_MESSAGE`), and
      // a commit message is repository content read by collaborators and CI — not
      // UI chrome — so it must not come from a translated catalog. It used to, so
      // a German dashboard wrote German subjects into a shared history.
      await papyrusApi.gitCommit(project as string, message)
      return papyrusApi.gitPush(project as string)
    },
    onSuccess: () => invalidateGit(),
    onError: (err: Error) => { if (!isFlushAbort(err)) setError(err.message) },
  })

  const onCommitAndPushClick = useCallback(() => {
    // Same one-field prompt as "new file" — the right weight for one line of
    // text, and it is what makes the `add -A` scope visible before it happens.
    const message = window.prompt(i18nT('apps.papyrus.workspace.commit_message_prompt'))
    // `null` is Cancel and must abort; an empty string is "use the default".
    if (message === null) return
    pushMutation.mutate(message.trim())
  }, [pushMutation])

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
        // `false`: do NOT flush. The agent just wrote this file, so the browser
        // buffer is the stale copy — flushing would save it over the agent's edits.
        await reloadOpenFile(false)
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
  // Option objects, not just names: a new array identity on every render would
  // bust `SearchableSelect`'s filter memo, and `mainCandidates` is already stable.
  const mainOptions = useMemo(
    () => mainCandidates.map(file => ({ value: file, label: file })),
    [mainCandidates],
  )

  // Warn before the BROWSER discards the buffer — reload, tab close, back out of the
  // SPA entirely. The in-app exits (`closeProject`, `openFile`, `openFullChat`,
  // `createFileMutation`) all flush, but none of them runs when the user hits ⌘R or
  // closes the tab: React never unmounts in a way we can await, so the only hook the
  // platform offers is `beforeunload`.
  //
  // Deliberately a WARNING and not a save. `beforeunload` cannot await, so a flush
  // started here is not guaranteed to reach the disk — a half-written file would be
  // worse than the prompt. Letting the user cancel and press ⌘S is honest about what
  // the platform can do.
  //
  // Registered only while dirty, so it never interferes with an ordinary reload.
  useEffect(() => {
    if (!dirty) return
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      // Legacy assignment as well as `preventDefault()`: browsers disagree about which
      // one arms the dialog, and the string itself is ignored by all of them.
      event.returnValue = ''
      return ''
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [dirty])

  /** Leave for the full chat page, flushing the buffer first.
   *
   * Navigating away UNMOUNTS this page, and the editor buffer lives only in memory
   * until a save lands — so routing out without flushing does not "forget" the work,
   * it destroys it, with nothing on disk to recover from. Exactly what `closeProject`
   * below is careful about, reached by a different button.
   *
   * A failed flush ABORTS the navigation rather than trading the user's text for a
   * chat view: `saveMutation.onError` has already put the write failure on screen, and
   * staying put leaves the text visible and re-saveable.
   */
  const openFullChat = useCallback(async () => {
    if (!(await flushBuffer())) return
    navigate(`/chat?sid=${encodeURIComponent(slotKey || '')}`)
  }, [flushBuffer, navigate, slotKey])

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

        {/* The picker is a `<button>` now, not a `<select>`, and HTML-AAM
            computes a button's accessible name from its own content — a
            `<label for>` is NOT in that path the way it is for a `<select>`.
            So the name comes from `aria-label` and the visible text is a
            plain span; that also retires the `jsx-a11y/label-has-for`
            suppression this site used to need, because there is no longer a
            `<label>` for the rule to be wrong about.
            `SearchableSelect` rather than `SimpleSelect`: the candidates are
            every `.tex` file in the project's RECURSIVE walk (bounded at
            `MAX_PROJECT_FILES`), so they are nested paths sharing a common
            prefix — a Radix Select's first-letter typeahead cannot separate
            `chapters/01.tex` from `chapters/02.tex`. */}
        <span className="flex items-center gap-1.5 text-[12px] text-muted">
          {i18nT('apps.papyrus.workspace.main_document')}
          <SearchableSelect
            options={mainOptions}
            value={mainFile}
            onChange={file => setMainMutation.mutate(file)}
            disabled={setMainMutation.isPending || mainCandidates.length === 0}
            aria-label={i18nT('apps.papyrus.workspace.main_document')}
            // The trigger is `w-full`, so it needs a definite flex basis in this
            // wrapping toolbar; a path too long for it truncates inside the span.
            style={{ flex: '0 0 14rem' }}
          />
        </span>

        <span className="text-[12px] text-muted truncate">
          {dirty
            ? i18nT('apps.papyrus.workspace.editing_unsaved', { file: currentFile })
            : i18nT('apps.papyrus.workspace.editing', { file: currentFile })}
        </span>

        {hasConflict && (
          // The conflict has to be VISIBLE and have an exit. A silent read-only editor
          // whose saves fail would be worse than the overwrite it replaced.
          <span className="flex items-center gap-2 text-[12px] text-warning">
            <AlertTriangle className="lucide-inline" />
            {i18nT('apps.papyrus.workspace.co_author_conflict')}
            <Btn onClick={resolveConflict}>
              {i18nT('apps.papyrus.workspace.co_author_conflict_discard')}
            </Btn>
          </span>
        )}

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
            {/* "Download PDF", not "PDF": this is an `<a download>` styled like the
                `Log` toggle beside it, so a bare noun gave two adjacent controls no
                way to tell a download from a pane toggle. */}
            {i18nT('apps.papyrus.workspace.download_pdf')}
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
            <Btn onClick={onCommitAndPushClick} disabled={pushMutation.isPending}>
              {pushMutation.isPending
                ? <Loader2 className="lucide-inline animate-spin motion-reduce:animate-none" />
                : <ArrowUpFromLine className="lucide-inline" />}
              {i18nT('apps.papyrus.workspace.commit_and_push')}
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
                // Read-only while the shown text is not yet the SELECTED file's,
                // and while a pull is rewriting it on disk. Both are windows in
                // which a keystroke would attach the wrong content to the current
                // path: `currentFile` switches immediately but `buffer` only
                // catches up when the fetch lands, and a pull replaces the file
                // underneath an editor that is still showing the pre-pull text.
                // Removing the window beats reconciling afterwards — every
                // "preserve the edit" variant still has to choose which text wins.
                // `hasConflict` too: the co-author edited this file while the buffer
                // was dirty, so saves are refused until it is reconciled and further
                // typing would only accumulate work that cannot be written.
                // `createFileMutation.isPending` too: that mutation flushes the buffer,
                // awaits the create, then SWITCHES `currentFile` to the new file — so a
                // keystroke landing in that window is attached to a buffer the switch
                // then abandons. Same class as the pull window right beside it, and the
                // same remedy: remove the window rather than reconcile afterwards.
                readOnly={
                  contentIsStale
                  || pullMutation.isPending
                  || hasConflict
                  || createFileMutation.isPending
                  // Deleting the OPEN file is the same window as creating one, one branch
                  // over: `onSuccess` clears the dirty flag and switches `currentFile`, so
                  // a keystroke during the request is attached to a buffer that is about
                  // to be abandoned — and, because the flag is cleared, is dropped without
                  // even the unsaved-changes prompt. The condition covers every mutation
                  // that ends with a `setDirty(false)` plus a `setCurrentFile`, delete
                  // included.
                  || deleteFileMutation.isPending
                }
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
              <span>{compileDurationLabel(compileMs)}</span>
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
                  onOpenFull={openFullChat}
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
