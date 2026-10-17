/**
 * The chat-core Attach atom's staging hook (RFC chat-core extraction, P3-f
 * Attach): upload, staging and send-time recovery for composer attachments,
 * with no store and no transport of its own.
 *
 * A host passes three things and the hook owns the rest:
 *
 * - `granted` -- whether this surface may upload at all. The upload writes to
 *   the gateway's disk, so it is the host's explicit decision; a host that
 *   cannot prove the grant passes `false` and nothing is ever posted.
 * - `post` -- the POST that carries the multipart body. An app passes its
 *   scoped client (so its declared API paths gate the request); the hook never
 *   reaches a dashboard client or a Redux store.
 * - `deniedNotice` -- the text a refused paste or drop shows, so each surface
 *   can name its own way to a page that accepts files.
 *
 * Staged files belong to one composer: a `resetKey` change (a slot switch)
 * or unmount aborts the uploads in flight and drops a late result rather than
 * staging it on another session's composer. Send-time recovery is `clear`
 * (a send took the files) plus `restore` (the send failed: its files go back
 * in front of anything staged since) and `removeFile` (a late proof of
 * delivery takes a restored copy back out).
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ClipboardEvent, type DragEvent } from 'react'
import { carriesFiles } from '../../lib/fileDrag'
import { hasPlainClipboardText } from '../../components/composerPastePolicy'
import type { ResizeInfo } from '../../utils/resizeImage'
import { uploadComposerFiles } from '../../utils/composerUpload'
import { i18nT } from '../../i18n/t'

export interface ComposerAttachmentsOptions {
  /** Identity of the composer the staged files belong to (the slot key). */
  resetKey: string
  /** The host allows uploads. `false` posts nothing and shows `deniedNotice`. */
  granted: boolean
  /** Send the multipart body; resolve the response or throw on failure. */
  post: (body: FormData, signal: AbortSignal) => Promise<Response>
  /** The notice a refused paste, drop or pick shows. */
  deniedNotice: () => string
}

export interface ComposerAttachments {
  /** The host granted uploads, so the attach control may be shown. */
  granted: boolean
  /** Server paths of the staged files, in staging order. */
  files: string[]
  resizedInfo: Record<string, ResizeInfo>
  /** Uploads in flight; a send waits until this is false. */
  uploading: boolean
  /** Names of the files still uploading, in pick order, so the composer can
   *  show what is on its way before the server returns their paths. */
  uploadingNames: string[]
  /** A refusal or failure to show above the composer; '' for none. */
  notice: string
  dismissNotice: () => void
  addFiles: (files: File[]) => void
  removeFile: (path: string) => void
  /** Drop the staged files (after a send took them). */
  clear: () => void
  /** Put a failed send's files back in front of anything staged since. */
  restore: (paths: string[]) => void
  /** Stage files that are already on the gateway's disk (an `@` mention),
   *  after anything staged before. Nothing is uploaded, so this needs no
   *  upload grant. */
  stage: (paths: string[]) => void
  onPaste: (e: ClipboardEvent<HTMLTextAreaElement>) => void
  onDragOver: (e: DragEvent<HTMLElement>) => void
  onDrop: (e: DragEvent<HTMLElement>) => void
}

/** A thrown upload failure's reason: a JSON `error` in an HTTP error's body
 *  when there is one, else the error's own message. */
function failureReason(err: unknown): string {
  const status = (err as { status?: number }).status
  const raw = (err as { body?: string }).body
  let reason = err instanceof Error ? err.message : String(err)
  if (typeof status === 'number' && raw) {
    try { reason = (JSON.parse(raw) as { error?: string }).error || reason } catch { /* keep message */ }
  }
  return reason
}

export function useComposerAttachments({ resetKey, granted, post, deniedNotice }: ComposerAttachmentsOptions): ComposerAttachments {
  const [files, setFiles] = useState<string[]>([])
  const [resizedInfo, setResizedInfo] = useState<Record<string, ResizeInfo>>({})
  // One entry per upload request still running, with the names it carries.
  const [inFlight, setInFlight] = useState<Array<{ id: number; names: string[] }>>([])
  const nextUploadId = useRef(0)
  const [notice, setNotice] = useState('')
  const controllers = useRef(new Set<AbortController>())
  const generation = useRef(0)
  // Read at call time so a host passing inline arrows does not re-create
  // `addFiles` (and every handler built on it) on each render.
  const postRef = useRef(post)
  postRef.current = post
  const deniedRef = useRef(deniedNotice)
  deniedRef.current = deniedNotice

  useEffect(() => {
    const live = controllers.current
    generation.current += 1
    setFiles([])
    setResizedInfo({})
    setNotice('')
    setInFlight([])
    return () => { live.forEach(c => c.abort()); live.clear() }
  }, [resetKey])

  const addFiles = useCallback((list: File[]) => {
    if (!list.length) return
    if (!granted) {
      setNotice(deniedRef.current())
      return
    }
    setNotice('')
    const gen = generation.current
    const controller = new AbortController()
    controllers.current.add(controller)
    const uploadId = ++nextUploadId.current
    setInFlight(prev => [...prev, { id: uploadId, names: list.map(f => f.name) }])
    void (async () => {
      try {
        const res = await uploadComposerFiles(list, body => postRef.current(body, controller.signal))
        if (gen !== generation.current) return
        if (res.error) throw new Error(res.error)
        const paths = res.paths.filter((p): p is string => typeof p === 'string' && p !== '')
        const info = res.resizedByPath
        setFiles(prev => [...prev, ...paths.filter(p => !prev.includes(p))])
        setResizedInfo(prev => ({ ...prev, ...info }))
      } catch (err) {
        if (controller.signal.aborted || gen !== generation.current) return
        setNotice(i18nT('pages.chatPage.upload_failed_error', { error: failureReason(err) }))
      } finally {
        controllers.current.delete(controller)
        if (gen === generation.current) setInFlight(prev => prev.filter(u => u.id !== uploadId))
      }
    })()
  }, [granted])

  const uploadingNames = useMemo(() => inFlight.flatMap(u => u.names), [inFlight])

  const removeFile = useCallback((path: string) => {
    setFiles(prev => prev.filter(p => p !== path))
  }, [])

  const clear = useCallback(() => { setFiles([]); setResizedInfo({}) }, [])
  const restore = useCallback((paths: string[]) => {
    setFiles(prev => [...paths, ...prev.filter(p => !paths.includes(p))])
  }, [])
  const stage = useCallback((paths: string[]) => {
    setFiles(prev => [...prev, ...paths.filter(p => !prev.includes(p))])
  }, [])
  const dismissNotice = useCallback(() => setNotice(''), [])

  const onPaste = useCallback((e: ClipboardEvent<HTMLTextAreaElement>) => {
    const data = e.clipboardData
    const pasted = Array.from(data?.files ?? [])
    // A paste carrying text keeps the textarea's own behaviour, as in the main
    // chat: a macOS Office text copy also carries an image of the selection,
    // and taking that image would throw the text away.
    if (!pasted.length || (data && hasPlainClipboardText(data))) return
    e.preventDefault()
    addFiles(pasted)
  }, [addFiles])

  const onDragOver = useCallback((e: DragEvent<HTMLElement>) => {
    if (carriesFiles(e.dataTransfer)) e.preventDefault()
  }, [])

  // A file drop is always swallowed, granted or not: the browser's default
  // for a file dropped on a textarea is to navigate the page to that file.
  const onDrop = useCallback((e: DragEvent<HTMLElement>) => {
    if (!carriesFiles(e.dataTransfer)) return
    e.preventDefault()
    addFiles(Array.from(e.dataTransfer.files ?? []))
  }, [addFiles])

  return {
    granted, files, resizedInfo, uploading: inFlight.length > 0, uploadingNames, notice, dismissNotice,
    addFiles, removeFile, clear, restore, stage, onPaste, onDragOver, onDrop,
  }
}
