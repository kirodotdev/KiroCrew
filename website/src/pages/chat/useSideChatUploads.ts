import { useCallback, useLayoutEffect, useRef, type SetStateAction } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchDashboardConfig } from '../../api/dashboardConfigQuery'
import { addSideChatFiles, readSideChatDraft, writeSideChatDraft } from '../../chat-core/composer/sideChatDrafts'
import { useChatPaneUploads } from '../../components/useChatPaneUploads'
import { useAppDispatch } from '../../store'
import { sideSendStatus } from '../../store/chatSlice'
import { useComposerArrivals } from '../../utils/composerSendHolds'

/**
 * Side Chat's attachment uploads: the main composer's upload flow
 * (`useChatPaneUploads`: the mutation, cancel, the count and size pre-checks,
 * the per-key hint and error) run under a SIDE key of its own.
 *
 * The key is what keeps the two composers of one session apart. Send holds,
 * the cancel registry and the arrivals inbox are all keyed in
 * `composerSendHolds`, so an upload in the side composer holds only the side
 * Send, and an upload in the main composer of the same slot never holds it.
 *
 * Finished uploads land in the side key's arrivals inbox and are staged on the
 * slot's side draft (`sideChatDrafts`), where the question's text already
 * lives, so a chip survives the panel unmounting just as the text does. An
 * upload that finishes while the panel is gone waits in the inbox until the
 * panel is back. A failure reported while the panel shows another slot, or no
 * slot, goes to that slot's side send status rather than to a transcript.
 */

const SIDE_UPLOAD_PREFIX = 'side-composer:'

/** The composer-hold key of the slot's side composer. */
export function sideUploadKey(slot: string): string {
  return SIDE_UPLOAD_PREFIX + slot
}

function slotOfSideUploadKey(key: string): string {
  return key.slice(SIDE_UPLOAD_PREFIX.length)
}

export function useSideChatUploads(slot: string) {
  const dispatch = useAppDispatch()
  const key = sideUploadKey(slot)
  const keyRef = useRef(key)
  const mountedRef = useRef(false)
  // A layout effect, so the ref names the slot on screen from the same commit
  // that rebinds the panel (`useChatPaneDraft` gives the same reason).
  useLayoutEffect(() => {
    keyRef.current = key
    mountedRef.current = true
    return () => { mountedRef.current = false }
  }, [key])
  // A dropped folder becomes an `@path/` token in the question's text.
  const setInput = useCallback((next: SetStateAction<string>) => {
    writeSideChatDraft(slot, typeof next === 'function' ? next(readSideChatDraft(slot)) : next)
  }, [slot])
  const { data: dashCfg, error: dashCfgError } = useQuery<{ upload_max_mb?: number }>({
    queryKey: ['dashboardConfig'], queryFn: fetchDashboardConfig, staleTime: 30_000,
  })
  const reportOffscreen = useCallback((message: string, forKey: string) => {
    dispatch(sideSendStatus({ slot: slotOfSideUploadKey(forKey), error: message }))
  }, [dispatch])
  const uploads = useChatPaneUploads({
    slotKey: key, dispatch, slotKeyRef: keyRef, mountedRef, setInput, dashCfg, dashCfgError, reportOffscreen,
  })
  useComposerArrivals(key, (paths, arrivalKey) => {
    addSideChatFiles(slotOfSideUploadKey(arrivalKey), paths)
    return true
  })
  return { ...uploads, uploadKey: key }
}
