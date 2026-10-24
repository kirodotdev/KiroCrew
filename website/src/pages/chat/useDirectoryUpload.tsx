import { useCallback, useEffect, useRef, useState, type ChangeEvent, type ReactNode } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import { i18nT } from '../../i18n/t'

export interface DirectoryUploadHandle {
  /** Upload one or more OS files into `absDir` (the tree root for a drop on
   *  the rail). A name that already exists in
   *  `absDir` is reported in `error`; the existing file is never replaced. */
  uploadFiles: (absDir: string, files: File[]) => Promise<void>
  /** Opens the OS file picker and uploads the selection into `absDir` — the
   *  "Upload files…" row-menu action's click path. */
  pickAndUpload: (absDir: string) => void
  /** The most recent upload failure, or null. */
  error: string | null
  dismissError: () => void
  /** Render once in the host's JSX: the hidden file-picker input. */
  fileInput: ReactNode
}

/**
 * Upload orchestration shared by the Files rail's drop overlay and its
 * "Upload files…" row-menu action — one place for the collision message, the
 * query invalidation that makes a new file appear in every file listing and
 * search, and the error surface, so the two entry points cannot drift apart.
 */
export function useDirectoryUpload(projectDir: string): DirectoryUploadHandle {
  const qc = useQueryClient()
  const [error, setError] = useState<string | null>(null)
  // A chat-slot switch changes projectDir in place without remounting the
  // rail (React's adjust-state-on-prop pattern, synchronous before paint):
  // one project's failure notice must not show in another project's panel.
  const [errorDir, setErrorDir] = useState(projectDir)
  if (errorDir !== projectDir) {
    setErrorDir(projectDir)
    setError(null)
  }
  // The project the rail shows now; an upload batch that finishes after a
  // switch reads it to keep its failures out of the new project's panel.
  const currentDirRef = useRef(projectDir)
  useEffect(() => {
    currentDirRef.current = projectDir
  }, [projectDir])
  const inputRef = useRef<HTMLInputElement | null>(null)
  const pendingDirRef = useRef<string | null>(null)

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

  const uploadFiles = useCallback(async (absDir: string, files: File[]) => {
    if (files.length === 0) return
    setError(null)
    const failures: string[] = []
    for (const file of files) {
      const failure = await uploadOne(absDir, file)
      if (failure) failures.push(failure)
    }
    if (failures.length > 0 && currentDirRef.current === projectDir) {
      setError(failures.join('; '))
    }
    invalidate()
  }, [uploadOne, invalidate, projectDir])

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
    error,
    dismissError: () => setError(null),
    fileInput,
  }
}
