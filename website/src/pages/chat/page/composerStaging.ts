import { useCallback, useMemo, useRef, useState, type MutableRefObject } from 'react'

import { useComposerPastes, type ComposerPasteSlice } from '../../../chat-core/composer/composerPastes'
import { useComposerDraftSelector, type ComposerDraftStore } from '../../../chat-core/composer/draftStore'
import { parseDirTokens } from '../../../utils/fileTokens'
import type { ResizeInfo } from '../../../utils/resizeImage'
import { addSessionRef, removeSessionRef, type SessionRef } from '../../../utils/sessionRefs'
import { useFileMentionTokens, type PickedFileTokens } from './composerFileMentions'

/** The staged folder refs as one string, so an unchanged set compares equal. */
const dirTokensKey = (text: string) => parseDirTokens(text).map(t => t.rel).join('\0')

interface ComposerStagingOptions {
  activeSlot: string | null
  splitMode: boolean
  composerSlotRef: MutableRefObject<string | null>
  /** The persisted picked-file alias store (`useComposerDraftStores`). */
  pickedFileTokens: PickedFileTokens
}

/**
 * What the composer holds besides its text: staged files (with the `@rel`
 * aliases a pick recorded), collapsed paste blocks, dropped session references,
 * the snip in progress, and the upload notices.
 *
 * State only. The effects over it are the slot-change restore
 * (`useComposerDraftLifecycle`) and the live persistence
 * (`useStagedDraftPersistence`), which ChatPage calls where the inline effects
 * ran so their order is unchanged.
 */
export function useComposerStaging({ activeSlot, splitMode, composerSlotRef, pickedFileTokens: pickedFileTokensStore }: ComposerStagingOptions) {
  const [uploading, setUploading] = useState(false)
  const [pendingFiles, setPendingFiles] = useState<string[]>([])
  const { pickedFileTokens, currentSlotTokens, recordSlotToken, mergeSlotTokens } = useFileMentionTokens(composerSlotRef, pickedFileTokensStore)
  const [snipFrame, setSnipFrame] = useState<HTMLCanvasElement | null>(null)
  // The slot that INITIATED the current snip. getDisplayMedia + cropping is
  // async and the user may switch slots meanwhile, so the cropped image must
  // land in the slot that started the capture — not whatever is active when the
  // crop completes. Threaded into uploadFiles as an explicit target.
  const snipSlotRef = useRef<string | null>(null)
  const pendingFilesRef = useRef(pendingFiles)
  // Collapsed paste blocks backing the `[ Paste #N · M lines ]` tokens in
  // `input`: the Paste atom's state. Persisted per-slot via chatPasteDrafts
  // (localStorage, 30-day TTL) so they survive slot switches / refresh;
  // cleared on send and slot delete. The page reads them through its own
  // `pasteBlocksRef`, which the staged persistence effect syncs -- not the
  // atom's render-time `read()` -- and hands the editor only the slice.
  const { blocks: pasteBlocks, set: setPasteBlocks } = useComposerPastes()
  const pasteBlocksRef = useRef(pasteBlocks)
  const pasteSlice = useMemo<ComposerPasteSlice>(() => ({ blocks: pasteBlocks, set: setPasteBlocks }), [pasteBlocks, setPasteBlocks])
  // Session references staged by dragging a session from the list onto this
  // pane. Serialized as LINKS on send — never the referenced transcript.
  const [pendingSessions, setPendingSessions] = useState<SessionRef[]>([])
  const pendingSessionsRef = useRef(pendingSessions)
  // Render-current staged resources for the slot-change effect. The three refs
  // above sync in effects declared AFTER that effect, so within one commit it
  // would read the previous render's files, pastes and session refs. This one is
  // written during render, so every effect sees the current values.
  const stagedNowRef = useRef({ files: pendingFiles, pastes: pasteBlocks, sessions: pendingSessions })
  stagedNowRef.current = { files: pendingFiles, pastes: pasteBlocks, sessions: pendingSessions }
  /** Stage a dropped session. Ignores duplicates and overflow (addSessionRef
   *  returns the same array, so this is a no-op re-render-free path). */
  const stageSessionRef = useCallback((ref: SessionRef) => {
    setPendingSessions(prev => addSessionRef(prev, ref))
  }, [])
  const unstageSessionRef = useCallback((key: string) => {
    setPendingSessions(prev => removeSessionRef(prev, key))
  }, [])
  /**
   * Whether a dropped session reference has a composer to land in.
   *
   * This predicate exists because the same defect appeared on three separate
   * surfaces: a drop is accepted, `pendingSessions` is set, and nothing ever
   * renders it — a silent black hole. Naming the condition once means a fourth
   * surface cannot quietly reintroduce it.
   *
   *  - `splitMode`: SessionGridView renders its own ChatInput per cell and
   *    ChatPage's composer is unmounted.
   *  - no `activeSlot`: ChatPage renders an empty state instead of a composer,
   *    the per-slot persist effect has no key to write under, and the
   *    slot-restore effect resets `pendingSessions` to `[]` on the next
   *    activation — so the ref is discarded rather than merely hidden.
   *
   * (embed 'sessions' mode needs no clause: it renders no chat pane at all, so
   * there is no `chatPaneEl` to hand over.)
   */
  const canStageSessionRef = !splitMode && !!activeSlot
  // The chat pane element, held in STATE (not a ref) because ChatSidebar portals
  // its drop zone into it — a ref's assignment does not re-render, so the portal
  // would never mount on the first paint.
  const [chatPaneEl, setChatPaneEl] = useState<HTMLDivElement | null>(null)
  // Two states, not one: `uploadError` is a FAILED request (the server's error
  // body, a thrown upload, a capture that could not complete) and renders
  // through ErrorNotice; `uploadHint` is the pre-flight validation the page
  // itself decided (too many files, file too large) — nothing was attempted, so
  // it stays plain status text.
  const [uploadError, setUploadError] = useState('')
  const [uploadHint, setUploadHint] = useState('')
  // Resize details keyed by uploaded server path. Rendered as a badge on the
  // attachment chip itself (FilePreviewStrip) instead of a banner — the info
  // describes one staged file, so it lives on that file's chip. Keyed by the
  // unique upload path, entries stay valid across slot switches (drafts
  // restore chips per slot) and stale keys are harmless.
  const [resizedInfo, setResizedInfo] = useState<Record<string, ResizeInfo>>({})
  return {
    uploading, setUploading,
    pendingFiles, setPendingFiles, pendingFilesRef,
    pickedFileTokens, currentSlotTokens, recordSlotToken, mergeSlotTokens,
    snipFrame, setSnipFrame, snipSlotRef,
    pasteBlocks, setPasteBlocks, pasteBlocksRef, pasteSlice,
    pendingSessions, setPendingSessions, pendingSessionsRef,
    stagedNowRef,
    stageSessionRef, unstageSessionRef, canStageSessionRef,
    chatPaneEl, setChatPaneEl,
    uploadError, setUploadError, uploadHint, setUploadHint,
    resizedInfo, setResizedInfo,
  }
}

export type ComposerStaging = ReturnType<typeof useComposerStaging>

/**
 * The staged folder references, derived from the composer text (see below).
 * ChatPage calls this where the inline selector sat, so the draft store's
 * subscription order is unchanged.
 */
export function useStagedFolderRefs(composerDraft: ComposerDraftStore): string[] {
  // Staged folder chips DERIVE from the composer text: an `@rel/` token is the
  // only form of a folder reference the agent receives, so token presence is
  // the single source of truth. There is no parallel state to leak across
  // slots, clear on send, or sync against hand-edits — inserting the token
  // stages the chip, deleting the token (by any means) unstages it, and the
  // per-slot text draft persists the reference across slot switches for free.
  const pendingDirsKey = useComposerDraftSelector(composerDraft, dirTokensKey)
  const pendingDirs = useMemo(() => (pendingDirsKey ? pendingDirsKey.split('\0') : []), [pendingDirsKey])
  return pendingDirs
}
