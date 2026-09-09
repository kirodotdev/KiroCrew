import { useCallback, useRef, useState, type ChangeEvent, type ReactNode } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import { useConfirm } from '../../components/ConfirmDialog'
import { i18nT } from '../../i18n/t'

export interface DirectoryUploadHandle {
  /** Upload one or more OS files into `absDir` (a drag-and-drop target, or
   *  the tree root for a pane-wide drop). Prompts once per name collision
   *  (Replace / Cancel) rather than silently overwriting. */
  uploadFiles: (absDir: string, files: File[]) => Promise<void>
  /** Opens the OS file picker and uploads the selection into `absDir` — the
   *  "Upload files…" row-menu action's click path. */
  pickAndUpload: (absDir: string) => void
  /** The most recent upload failure, or null. */
  error: string | null
  dismissError: () => void
  /** Render once in the host's JSX: the hidden file-picker input. */
  fileInput: ReactNode
  /** Render once in the host's JSX: the collision-confirm dialog. */
  confirmDialog: ReactNode
}

/**
 * Upload orchestration shared by the Files rail's drag-and-drop, its
 * full-pane drop overlay, and its "Upload files…" row-menu action — one
 * place for the collision prompt, the query invalidation that makes a new
 * file appear in the tree, and the error surface, so none of the three entry
 * points can drift from the other two.
 */
export function useDirectoryUpload(projectDir: string): DirectoryUploadHandle {
  const qc = useQueryClient()
  const { confirm, confirmDialog } = useConfirm()
  const [error, setError] = useState<string | null>(null)
  const inputRef = useRef<HTMLInputElement | null>(null)
  const pendingDirRef = useRef<string | null>(null)

  const invalidate = useCallback(() => {
    qc.invalidateQueries({ queryKey: ['project-tree', projectDir] })
    qc.invalidateQueries({ queryKey: ['git-status', projectDir] })
  }, [qc, projectDir])

  // The actual write is a server-state mutation, so it goes through
  // useMutation/mutateAsync rather than a bare fetch call (website/AGENTS.md:
  // "Data fetching is React Query"). One mutation covers both the fresh
  // attempt and the overwrite retry below -- they differ only in `opts`, not
  // in what's being mutated.
  const uploadMutation = useMutation({
    mutationFn: ({ absDir, file, opts }: { absDir: string; file: File; opts?: { overwrite?: boolean } }) =>
      // Keep the call arity identical to the pre-mutation call sites (two
      // args for a fresh attempt, three for an overwrite retry) rather than
      // always passing `opts` through as an explicit third argument —
      // callers and tests alike distinguish "no options" from "an explicit
      // undefined".
      opts ? api.uploadToDirectory(absDir, file, opts) : api.uploadToDirectory(absDir, file),
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
    absDir: string, file: File, opts?: { overwrite?: boolean },
  ): Promise<Awaited<ReturnType<typeof api.uploadToDirectory>>> => {
    try {
      return await uploadMutation.mutateAsync({ absDir, file, opts })
    } catch (err) {
      return { ok: false, status: 0, error: err instanceof Error ? err.message : String(err) }
    }
  }, [uploadMutation])

  const uploadOne = useCallback(async (absDir: string, file: File) => {
    let result = await attemptUpload(absDir, file)
    if (!result.ok && result.code === 'name_collision') {
      const replace = await confirm({
        title: i18nT('pages.chat.fileBrowserRail.overwrite_title'),
        body: i18nT('pages.chat.fileBrowserRail.overwrite_body', { name: file.name }),
        confirmLabel: i18nT('pages.chat.fileBrowserRail.overwrite_action'),
      })
      if (!replace) return
      result = await attemptUpload(absDir, file, { overwrite: true })
    }
    if (!result.ok) {
      setError(
        `${i18nT('pages.chat.fileBrowserRail.upload_failed', { name: file.name })}: ${result.error}`,
      )
    }
  }, [confirm, attemptUpload])

  const uploadFiles = useCallback(async (absDir: string, files: File[]) => {
    if (files.length === 0) return
    for (const file of files) {
      // Sequential, not Promise.all: a collision confirm for file N must
      // settle before file N+1's own (potential) confirm opens, or the
      // second dialog would replace the first mid-answer.
      await uploadOne(absDir, file)
    }
    invalidate()
  }, [uploadOne, invalidate])

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
    confirmDialog,
  }
}
