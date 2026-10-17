import { useState, useCallback, type Dispatch, type MutableRefObject, type SetStateAction } from 'react'
import { useMutation } from '@tanstack/react-query'
import { useChatFileDrop } from './ChatDropOverlay'
import { appendSlotMessage } from '../store/chatSlice'
import { api } from '../api/client'
import { classifyDrop } from '../utils/dropClassify'
import { spliceDirTokens, servedUploadMb, VIDEO_EXT } from '../utils/fileTokens'
import {
  cancelComposerUploads,
  holdComposerSend,
  registerComposerUpload,
  composerUploadSlot,
  finishComposerAttachment,
  unregisterComposerUpload,
  useComposerSendHeld,
  useComposerUploadCount,
} from '../utils/composerSendHolds'
import { i18nT } from '../i18n/t'
import type { AppDispatch } from '../store'

/**
 * `ChatPane`'s attachment uploads: the upload mutation (its result follows the
 * slot the files were picked in, not the screen), cancel, the count and size
 * pre-checks, folder drops, and the per-slot upload notices: a refusal hint and
 * a failure banner keyed by slot, or an error row in the slot's transcript
 * when that slot is no longer on screen.
 */

/** Variables of the composer upload mutation: the files, the slot they were
 *  picked in, and the AbortController that can end the request. */
type UploadVars = { files: File[]; forSlot: string; controller: AbortController }

export function useChatPaneUploads({ slotKey, dispatch, slotKeyRef, mountedRef, setInput, dashCfg, dashCfgError }: {
  slotKey: string
  dispatch: AppDispatch
  slotKeyRef: MutableRefObject<string>
  mountedRef: MutableRefObject<boolean>
  setInput: Dispatch<SetStateAction<string>>
  dashCfg: { upload_max_mb?: number } | undefined
  dashCfgError: unknown
}) {
  // Upload failures shown as a banner, keyed by the slot they were shown for.
  // A banner belongs to the conversation it happened in: rebinding the pane to
  // another slot must not carry A's banner over B's thread, and coming back to
  // A shows it again until dismissed. Dismiss clears only the shown slot's
  // entry. A failure for a slot NOT on screen never enters this map — see
  // reportUploadFailure, which writes it to that slot's transcript instead.
  const [uploadErrors, setUploadErrors] = useState<Record<string, string>>({})
  const uploadError = uploadErrors[slotKey] ?? ''
  // Client-side pre-check refusals (too many files, over the size limit) are
  // validation hints, not failures: nothing was sent. They render through
  // StatusNotice, keyed per slot exactly like the banner below.
  const [uploadHints, setUploadHints] = useState<Record<string, string>>({})
  const uploadHint = uploadHints[slotKey] ?? ''
  const setUploadHint = useCallback((message: string) => {
    setUploadHints(prev => {
      if (!message) { if (!(slotKey in prev)) return prev; const next = { ...prev }; delete next[slotKey]; return next }
      return { ...prev, [slotKey]: message }
    })
  }, [slotKey])
  const setUploadError = useCallback((message: string, forSlot: string = slotKey) => {
    setUploadErrors(prev => {
      if (!message) { if (!(forSlot in prev)) return prev; const next = { ...prev }; delete next[forSlot]; return next }
      return { ...prev, [forSlot]: message }
    })
  }, [slotKey])
  /** Report an upload failure to the slot whose files failed. On screen it is
   *  the banner. Anywhere else — the pane rebound to another slot, or gone —
   *  it goes into that slot's TRANSCRIPT as an error row, the same place a
   *  failed send reports: the transcript is store-backed and survives both the
   *  rebind and leaving the page, so the failure is found on return rather
   *  than held in component state nobody may ever render. Never dropped. */
  const reportUploadFailure = useCallback((message: string, forSlot: string) => {
    if (mountedRef.current && forSlot === slotKeyRef.current) { setUploadError(message, forSlot); return }
    dispatch(appendSlotMessage({ slot: forSlot, message: { role: 'error', content: message, cls: '' } }))
  }, [dispatch, setUploadError, mountedRef, slotKeyRef])

  // File upload as a mutation (isPending replaces a manual `uploading` flag).
  //
  // The variables carry the slot the files were picked in: the pane can be
  // rebound to another slot while the upload is in flight, and the result
  // must follow the files' slot, not the screen: completed paths enter that
  // slot's arrivals inbox, while failures reach its banner or transcript.
  //
  // The variables also carry the request's own AbortController. It is
  // registered per slot in the shared composerSendHolds registry, not kept on
  // this pane: the hold below outlives the pane, so a pane remounted mid-upload
  // must be able to cancel a request it did not start, or a hung request leaves
  // its Send dead. The registry holds EVERY live one, since paste, a drop and a
  // Sketch insert all reach `uploadFiles` ungated and two can be in flight.
  //
  // Shared per-slot holds survive a pane remount. Mutation callbacks still
  // release their hold after the pane that started the upload unmounts.
  const sendHeld = useComposerSendHeld(slotKey)
  const uploadCount = useComposerUploadCount(slotKey)
  const uploadMutation = useMutation({
    // 'always', not the default 'online': an offline browser would PAUSE the
    // mutation before mutationFn, so onSettled (the only release of the shared
    // hold below) would never run and the slot's Send would stay dead. Running
    // it lets the fetch fail fast and the hold clear through the usual path.
    networkMode: 'always',
    mutationFn: ({ files, controller }: UploadVars) => api.uploadFiles(files, controller.signal),
    // api.uploadFiles does NOT throw on a server refusal (unsupported type,
    // signature mismatch, over-cap): it resolves with { paths: [], error }.
    // So a refusal lands here in onSuccess, not onError — surface res.error
    // (matching ChatPage) instead of silently doing nothing.
    onSuccess: (res, { forSlot }) => {
      if (res.error) reportUploadFailure(i18nT('pages.chatPage.upload_failed_error', { error: res.error }), forSlot)
    },
    // api.uploadFiles throws for three distinct reasons: a client-side image
    // resize failure, a session expiry, and a transport reject. The first two
    // carry a message worth showing. A fetch reject arrives as a TypeError
    // reading "Failed to fetch", which is not user-facing copy, so that case
    // gets the pane's shared connectivity string instead.
    onError: (err: unknown, { forSlot }) => {
      // A cancel the user asked for is not a failure: it raises no banner.
      if ((err as Error | undefined)?.name === 'AbortError') return
      const message = (err as Error)?.message
      const reason = (!message || err instanceof TypeError)
        ? i18nT('pages.chatPage.connection_error')
        : message
      reportUploadFailure(i18nT('pages.chatPage.upload_failed_error', { error: reason }), forSlot)
    },
    onSettled: (data, _err, { controller, forSlot }: UploadVars) => {
      // Where the upload lives now: a memory-mode switch may have handed it,
      // with its hold, to the replacement session while it ran.
      const landing = composerUploadSlot(controller) ?? forSlot
      unregisterComposerUpload(landing, controller)
      finishComposerAttachment(landing, data && !data.error ? data.paths ?? [] : [])
    },
  })
  /** Abort every upload in flight for this pane's slot, whichever host started it. */
  const cancelUpload = useCallback(() => {
    cancelComposerUploads(slotKeyRef.current)
  }, [slotKeyRef])
  const uploadFiles = useCallback((files: File[]) => {
    if (!files.length) return
    // Clear FIRST, so a refusal from the previous attempt cannot stay on
    // screen and read as the reason this one failed.
    setUploadError('')
    setUploadHint('')
    if (files.length > 20) { setUploadHint(i18nT('pages.chatPage.too_many_files_max_20')); return }
    // Video is deliberately exempt from this pre-check, exactly as in
    // ChatPage: the server's video ceiling is far higher than the per-file
    // cap, so the figure this message states would be a lie for a recording.
    // An over-cap recording's own 413 carries the real cap and surfaces
    // through the res.error branch above -- the route every other server-side
    // refusal already takes. The cap is the gateway's `dashboard.upload_max_mb`;
    // until the config has served a usable figure there is nothing to check
    // against, so the server's own 413 is the only check.
    const maxMb = servedUploadMb(dashCfg?.upload_max_mb)
    const big = maxMb === null ? undefined : files.find((f) => !VIDEO_EXT.test(f.name) && f.size > maxMb * 1024 * 1024)
    if (big) { setUploadHint(i18nT('pages.chatPage.file_too_large', { name: big.name, max: maxMb })); return }
    // A failed settings read is reported beside the attempt it affects, not in
    // every pane: the files still go to the server, whose 413 replaces this
    // banner if one is refused. The copy stays true once the upload finishes.
    // No hand-off: same unsaved composer draft.
    if (maxMb === null && dashCfgError) setUploadError(i18nT('pages.chatPage.upload_limit_unavailable'))
    const controller = new AbortController()
    const forSlot = slotKeyRef.current
    holdComposerSend(forSlot)
    registerComposerUpload(forSlot, controller)
    uploadMutation.mutate({ files, forSlot, controller })
  }, [uploadMutation, setUploadError, setUploadHint, dashCfg, dashCfgError, slotKeyRef])

  // Classify BEFORE acting (issue #743): a dropped folder inserts its path
  // into the composer as an `@path/` token instead of taking the upload
  // route, which cannot ingest a directory. Files keep uploading; a mixed
  // drop takes both routes. The pane has no project context, so the token
  // keeps the absolute path (the picker's own out-of-root fallback form),
  // appended — the pane does not track a live composer caret. In a plain
  // browser no real path is visible, so classifyDrop leaves folders on the
  // upload route there (today's behaviour).
  const handleDrop = useCallback((dataTransfer: DataTransfer) => {
    const { files, dirPaths } = classifyDrop(dataTransfer)
    if (dirPaths.length) setInput((prev) => spliceDirTokens(prev, null, dirPaths).value)
    if (files.length) uploadFiles(files)
  }, [uploadFiles, setInput])
  const { active: dragOver, dropTargetProps } = useChatFileDrop(handleDrop)

  return {
    uploadError, setUploadError, uploadHint, setUploadHint, sendHeld, uploadCount, uploadMutation,
    cancelUpload, uploadFiles, dragOver, dropTargetProps,
  }
}
