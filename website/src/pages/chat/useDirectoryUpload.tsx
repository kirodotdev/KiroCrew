import { useCallback, useEffect, useRef, useState, type ChangeEvent, type ReactNode } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import { i18nT } from '../../i18n/t'
import { backslashIsSeparator, pathBasename, stripTrailingSeparators } from '../../utils/pathBasename'

export interface DirectoryUploadHandle {
  /** Upload one or more OS files into `absDir` (the tree root for a drop on
   *  the rail). A name that already exists in
   *  `absDir` is reported in `error`; the existing file is never replaced.
   *  A name already uploading into `absDir` is skipped, not sent again. */
  uploadFiles: (absDir: string, files: File[]) => Promise<void>
  /** Opens the OS file picker and uploads the selection into `absDir` — the
   *  "Upload files…" row-menu action's click path. */
  pickAndUpload: (absDir: string) => void
  /** "Uploading report.md…" / "Uploading 3 files…" while a batch runs, or null. */
  progress: string | null
  /** The most recent upload failure, or null. */
  error: string | null
  dismissError: () => void
  /** "Uploaded report.md to docs." after a batch with no failures, or null. */
  success: string | null
  dismissSuccess: () => void
  /** Render once in the host's JSX: the hidden file-picker input. */
  fileInput: ReactNode
}

/** `p` with `/` separators and no trailing separator, for comparing paths. */
function comparablePath(p: string): string {
  return stripTrailingSeparators(backslashIsSeparator(p) ? p.replace(/\\/g, '/') : p)
}

interface RunningBatch {
  id: number
  names: string[]
}

/**
 * Upload orchestration shared by the Files rail's drop overlay and its
 * "Upload files…" row-menu action — one place for the collision message, the
 * query invalidation that makes a new file appear in every file listing and
 * search, and the progress, success and error surface, so the two entry points
 * cannot drift apart.
 *
 * `rootDir` is the directory the tree lists (the backend's realpath of
 * `projectDir`, which can differ for a symlinked project); the success line
 * names a target folder relative to it, and the root itself by the project's
 * folder name.
 */
export function useDirectoryUpload(projectDir: string, rootDir: string = projectDir): DirectoryUploadHandle {
  const qc = useQueryClient()
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const [running, setRunning] = useState<RunningBatch[]>([])
  // A chat-slot switch changes projectDir in place without remounting the
  // rail (React's adjust-state-on-prop pattern, synchronous before paint):
  // one project's notices must not show in another project's panel.
  const [errorDir, setErrorDir] = useState(projectDir)
  if (errorDir !== projectDir) {
    setErrorDir(projectDir)
    setError(null)
    setSuccess(null)
    setRunning([])
  }
  // The project the rail shows now; an upload batch that finishes after a
  // switch reads it to keep its failures out of the new project's panel.
  const currentDirRef = useRef(projectDir)
  useEffect(() => {
    currentDirRef.current = projectDir
  }, [projectDir])
  const inputRef = useRef<HTMLInputElement | null>(null)
  const pendingDirRef = useRef<string | null>(null)
  // `${dir}/${name}` of every file being sent now, across batches.
  const inFlightRef = useRef(new Set<string>())
  const batchIdRef = useRef(0)

  // Every cached listing or search of workspace files an upload can add a row
  // to. The first three are keyed by `projectDir`. The rest are keyed by a
  // root this hook cannot know, so they are invalidated by prefix: the rail's
  // filename search uses the backend's realpath of the project, and the folder
  // tab's listing and search use whatever folder that tab is showing.
  const invalidate = useCallback(() => {
    qc.invalidateQueries({ queryKey: ['project-tree', projectDir] })
    qc.invalidateQueries({ queryKey: ['git-status', projectDir] })
    qc.invalidateQueries({ queryKey: ['file-grep', projectDir] })
    qc.invalidateQueries({ queryKey: ['rail-name-search'] })
    qc.invalidateQueries({ queryKey: ['browse-files'] })
    qc.invalidateQueries({ queryKey: ['folder-file-search'] })
  }, [qc, projectDir])

  // The actual write is a server-state mutation, so it goes through
  // useMutation/mutateAsync rather than a bare fetch call (website/AGENTS.md:
  // "Data fetching is React Query").
  const uploadMutation = useMutation({
    mutationFn: ({ absDir, file }: { absDir: string; file: File }) =>
      api.uploadToDirectory(absDir, file),
  })

  // api.uploadToDirectory wraps fetch(); a transport failure (offline, DNS,
  // an aborted request) REJECTS that promise instead of resolving an
  // {ok:false} result -- and mutateAsync propagates that same rejection.
  // Every caller here fires uploadFiles without awaiting it
  // (`void uploadFiles(...)`), so an unwrapped rejection becomes an unhandled
  // promise rejection: no error state, no invalidate, and the drop or picker
  // selection appears to do nothing. Catching here folds a thrown error into
  // the SAME {ok:false} shape the API already returns for a non-2xx
  // response, so uploadOne's own handling covers both without a second
  // error path.
  const attemptUpload = useCallback(async (
    absDir: string, file: File,
  ): Promise<Awaited<ReturnType<typeof api.uploadToDirectory>>> => {
    try {
      return await uploadMutation.mutateAsync({ absDir, file })
    } catch (err) {
      return { ok: false, status: 0, error: err instanceof Error ? err.message : String(err) }
    }
  }, [uploadMutation])

  const uploadOne = useCallback(async (absDir: string, file: File): Promise<string | null> => {
    const result = await attemptUpload(absDir, file)
    if (result.ok) return null
    if (result.code === 'name_collision') {
      return i18nT('pages.chat.fileBrowserRail.upload_exists', { name: file.name })
    }
    return `${i18nT('pages.chat.fileBrowserRail.upload_failed', { name: file.name })}: ${result.error}`
  }, [attemptUpload])

  // The folder a success line names: its path under the tree root, or the
  // project's folder name (what the Files tab header shows) for the root.
  const folderLabel = useCallback((absDir: string): string => {
    const dir = comparablePath(absDir)
    const root = comparablePath(rootDir)
    if (dir === root || dir === comparablePath(projectDir)) {
      return pathBasename(comparablePath(projectDir)) || projectDir
    }
    if (dir.startsWith(`${root}/`)) return dir.slice(root.length + 1)
    return pathBasename(dir) || dir
  }, [rootDir, projectDir])

  const uploadFiles = useCallback(async (absDir: string, files: File[]) => {
    // A name still uploading into this folder (typically dropped again because
    // the first drop seemed to do nothing) is not sent a second time: the server
    // would refuse it as a collision with the copy it is still writing. The
    // running batch's progress line already covers it.
    const dirKey = comparablePath(absDir)
    const batch: File[] = []
    const keys: string[] = []
    for (const file of files) {
      const key = `${dirKey}/${file.name}`
      if (inFlightRef.current.has(key)) continue
      inFlightRef.current.add(key)
      keys.push(key)
      batch.push(file)
    }
    if (batch.length === 0) return
    const id = ++batchIdRef.current
    setError(null)
    setSuccess(null)
    setRunning(prev => [...prev, { id, names: batch.map(f => f.name) }])
    const failures: string[] = []
    for (const file of batch) {
      const failure = await uploadOne(absDir, file)
      if (failure) failures.push(failure)
    }
    for (const key of keys) inFlightRef.current.delete(key)
    setRunning(prev => prev.filter(b => b.id !== id))
    if (currentDirRef.current === projectDir) {
      if (failures.length > 0) {
        setError(failures.join('; '))
      } else {
        const folder = folderLabel(absDir)
        setSuccess(batch.length === 1
          ? i18nT('pages.chat.fileBrowserRail.uploaded_name', { name: batch[0].name, folder })
          : i18nT('pages.chat.fileBrowserRail.uploaded_count', { count: batch.length, folder }))
      }
    }
    invalidate()
  }, [uploadOne, invalidate, projectDir, folderLabel])

  const runningNames = running.flatMap(b => b.names)
  const progress = runningNames.length === 0
    ? null
    : runningNames.length === 1
      ? i18nT('pages.chat.fileBrowserRail.uploading_name', { name: runningNames[0] })
      : i18nT('pages.chat.fileBrowserRail.uploading_count', { count: runningNames.length })

  const pickAndUpload = useCallback((absDir: string) => {
    pendingDirRef.current = absDir
    inputRef.current?.click()
  }, [])

  const onInputChange = useCallback((e: ChangeEvent<HTMLInputElement>) => {
    const dir = pendingDirRef.current
    const files = e.target.files ? Array.from(e.target.files) : []
    // Reset so picking the SAME file again still fires a change event.
    e.target.value = ''
    if (dir && files.length) void uploadFiles(dir, files)
  }, [uploadFiles])

  const fileInput = (
    <input
      ref={inputRef}
      type="file"
      multiple
      className="hidden"
      aria-label={i18nT('pages.chat.fileBrowserRail.ctx_upload_files')}
      onChange={onInputChange}
    />
  )

  return {
    uploadFiles,
    pickAndUpload,
    progress,
    error,
    dismissError: () => setError(null),
    success,
    dismissSuccess: () => setSuccess(null),
    fileInput,
  }
}
