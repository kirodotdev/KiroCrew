import { useCallback, useEffect, useRef, useState } from 'react'

import { i18nT } from '../../i18n/t'
import { FileExplorerApiError, fileExplorerApi } from './api'

const AUTOSAVE_DEBOUNCE_MS = 1200
/** A switch-time flush that fails must not silently discard typed text. */
const RECOVERY_PREFIX = 'kc-fe-recovery:'

export interface MarkdownEditorState {
  editing: boolean
  buffer: string | null
  status: string
  statusIsWarning: boolean
  start: (content: string, mtime: number, mtimeNs?: number) => void
  /** Flush any pending change and leave edit mode. False = save failed. */
  finish: () => Promise<boolean>
  setBuffer: (value: string) => void
}

function statusFor(err: unknown): string {
  return err instanceof FileExplorerApiError && err.status === 409
    ? i18nT('apps.fileExplorer.editor.file_changed_on_disk')
    : i18nT('apps.fileExplorer.editor.save_failed')
}

/**
 * Markdown editing with debounced auto-save. Saves carry the mtime the
 * editor loaded (`base_mtime`), so if anything else wrote the file since —
 * another window, an agent filing notes — the backend answers 409 and the
 * user sees "file changed on disk" instead of silently losing that write.
 * A dirty buffer is also flushed when the file is switched or the component
 * unmounts.
 */
export function useMarkdownEditor(path: string | null): MarkdownEditorState {
  const [editing, setEditing] = useState(false)
  const [buffer, setBufferState] = useState<string | null>(null)
  const [status, setStatus] = useState('')
  const [statusIsWarning, setStatusIsWarning] = useState(false)
  const mtimeRef = useRef(0)
  const mtimeNsRef = useRef<number | undefined>(undefined)
  const lastSavedRef = useRef('')
  const editingRef = useRef(false)
  const bufferRef = useRef<string | null>(null)
  editingRef.current = editing
  bufferRef.current = buffer

  const note = useCallback((text: string, warning = false) => {
    setStatus(text)
    setStatusIsWarning(warning)
  }, [])

  // Leaving the file (or unmounting) flushes a dirty buffer fire-and-forget:
  // losing typed text to a tab switch is the one unacceptable outcome.
  useEffect(() => {
    if (!path) return
    const flushPath = path
    return () => {
      if (editingRef.current && bufferRef.current != null && bufferRef.current !== lastSavedRef.current) {
        const dirty = bufferRef.current
        fileExplorerApi.write(flushPath, dirty, mtimeRef.current, mtimeNsRef.current).catch(() => {
          // The write failed while the editor was going away: park the text
          // in localStorage so the next edit session can offer it back.
          try {
            localStorage.setItem(RECOVERY_PREFIX + flushPath, dirty)
          } catch {
            // Quota/private-mode failure leaves nothing else to do.
          }
        })
      }
    }
  }, [path])

  useEffect(() => {
    setEditing(false)
    setBufferState(null)
    note('')
  }, [path, note])

  useEffect(() => {
    if (!editing || !path || buffer == null || buffer === lastSavedRef.current) return
    note(i18nT('apps.fileExplorer.editor.saving'))
    const timer = setTimeout(async () => {
      try {
        const res = await fileExplorerApi.write(path, buffer, mtimeRef.current, mtimeNsRef.current)
        mtimeRef.current = res.mtime
        mtimeNsRef.current = res.mtime_ns
        lastSavedRef.current = buffer
        try {
          localStorage.removeItem(RECOVERY_PREFIX + path)
        } catch {
          // stash cleanup is best-effort
        }
        note(i18nT('apps.fileExplorer.editor.saved'))
      } catch (err) {
        note(statusFor(err), true)
      }
    }, AUTOSAVE_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [buffer, editing, path, note])

  const start = useCallback((content: string, mtime: number, mtimeNs?: number) => {
    let initial = content
    let recoveredOffer = false
    if (path) {
      // A previous session's failed switch-time flush left recovered text.
      // OFFER it (load into the buffer, flag the status) without writing:
      // the disk may have moved on, and auto-saving would clobber that
      // write — the user's next edit is the consent that saves it.
      try {
        const recovered = localStorage.getItem(RECOVERY_PREFIX + path)
        if (recovered != null && recovered !== content) {
          initial = recovered
          recoveredOffer = true
          note(i18nT('apps.fileExplorer.editor.recovered_unsaved'), true)
        } else {
          localStorage.removeItem(RECOVERY_PREFIX + path)
        }
      } catch {
        // localStorage unavailable: edit proceeds from the file content.
      }
    }
    setBufferState(initial)
    // Recovered text is marked as already-saved so the autosave debounce
    // does NOT fire until the user actually edits.
    lastSavedRef.current = initial
    mtimeRef.current = mtime || 0
    mtimeNsRef.current = mtimeNs
    if (!recoveredOffer) note('')
    setEditing(true)
  }, [note, path])

  const finish = useCallback(async () => {
    if (path && buffer != null && buffer !== lastSavedRef.current) {
      try {
        note(i18nT('apps.fileExplorer.editor.saving'))
        const res = await fileExplorerApi.write(path, buffer, mtimeRef.current, mtimeNsRef.current)
        mtimeRef.current = res.mtime
        mtimeNsRef.current = res.mtime_ns
        lastSavedRef.current = buffer
      } catch (err) {
        note(statusFor(err), true)
        return false
      }
    }
    setEditing(false)
    note('')
    return true
  }, [path, buffer, note])

  return { editing, buffer, status, statusIsWarning, start, finish, setBuffer: setBufferState }
}

export function MarkdownEditor({ editor, fileName }: { editor: MarkdownEditorState; fileName: string }) {
  return (
    <textarea
      value={editor.buffer ?? ''}
      onChange={(e) => editor.setBuffer(e.target.value)}
      spellCheck={false}
      autoFocus
      aria-label={i18nT('apps.fileExplorer.editor.edit_file', { name: fileName })}
      style={{ width: '100%', height: '100%', minHeight: '100%', border: 0, outline: 'none', resize: 'none', background: 'transparent', color: 'inherit', font: '13px/1.55 ui-monospace, SFMono-Regular, Menlo, monospace', padding: '14px 18px', boxSizing: 'border-box', display: 'block' }}
    />
  )
}
