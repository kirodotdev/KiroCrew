/**
 * Wires the prompt stash (`utils/promptStash.ts`) to one composer.
 *
 * The host passes its controlled draft (text + paste blocks) and setters; the
 * hook returns a keydown handler to attach to the element that wraps the
 * composer, `stash` / `restoreLatest` actions for menu items, the current
 * stack size for the status label, a short visible notice, and a live-region
 * message describing the last stash action for screen readers.
 *
 * The handler claims Cmd/Ctrl+S ONLY for keystrokes inside the composer, and
 * only when it acts (stash, restore, refusal at the cap or over attachments):
 * then it calls `preventDefault` (no browser "save page" dialog) and
 * `stopPropagation` (document-level Cmd/Ctrl+S handlers, such as an artifact
 * editor's save, do not also fire). A no-op press reaches document-level
 * handlers unprevented, and only its browser default is cancelled afterwards,
 * at `window`. Outside the composer the chord is untouched. With no slot, or
 * while the composer is disabled/read-only, the chord is not claimed.
 *
 * Attachments (files, folders, session references) are not stashed: with any
 * pending, both actions refuse and leave the draft and the stack untouched,
 * so the chord can neither restore text over a user's files nor stash the text
 * while leaving the files behind.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import type React from 'react'
import { i18nT } from '../i18n/t'
import type { PasteBlock } from '../utils/pasteTokens'
import {
  addStashEntry,
  PROMPT_STASH_MAX,
  PROMPT_STASH_MAX_BYTES,
  isDraftEmpty,
  isStashChord,
  loadPromptStash,
  makeStashEntry,
  planStashChord,
  promptStashBytes,
  promptStashKey,
  removeStashEntry,
  stashWriteFits,
  withPromptStashLock,
} from '../utils/promptStash'

export interface UsePromptStashOptions {
  /** Chat slot the stack belongs to. Falsy disables the feature. */
  slotKey: string | null | undefined
  value: string
  blocks: PasteBlock[]
  /** Receives the hook's programmatic writes (clear on stash, restore, swap).
   *  A host with its own undo history should treat these as a new base, not
   *  an undoable step. */
  onChange: (value: string) => void
  onBlocksChange?: (blocks: PasteBlock[]) => void
  /** Reseeds host-owned undo history after a programmatic replacement. Called
   *  even when the replacement text is unchanged, because its paste blocks may
   *  still have changed. */
  onReseed?: (value: string, blocks: PasteBlock[]) => void
  /** Disabled or read-only composer: the chord is left alone. */
  inert?: boolean
  /** Pending attachments (files, folders, session refs). Any refuses both actions. */
  attachmentCount?: number
  /** Chord label shown in messages, e.g. `⌘S` or `Ctrl+S`. */
  chordLabel: string
  /** Touch / coarse-pointer composer: there is no Cmd/Ctrl to press, so any
   *  message that would tell the user to press the chord points at the menu
   *  instead. The host passes the input method (`isTouchDevice()`), not the
   *  narrow-viewport signal that mounts the overflow: a keyboard user in a
   *  narrow window can still press the chord. */
  touch?: boolean
}

export type PromptStashDisabledReason = 'full' | 'attachments' | 'empty' | null

export interface PromptStashState {
  count: number
  full: boolean
  /** `stash` would act: non-empty draft, no attachments, stack not full. */
  canStash: boolean
  /** Why the menu's stash row is disabled, when the draft state explains it. */
  stashDisabledReason: PromptStashDisabledReason
  /** `restoreLatest` would act: something stashed, no attachments. */
  canRestore: boolean
  /** A restore now would swap: the composer's current draft is stashed in its place. */
  restoreSwaps: boolean
  /** Text for a polite live region; set on every stash action. */
  announcement: string
  /** Bumped with every `announcement`, including one whose text repeats the
   *  last. Render it as the `key` of the node holding the text so a repeated
   *  refusal replaces the text node and is announced again. */
  announcementKey: number
  /** Set when a stash could not be written to storage; the draft was kept. */
  error: string
  /** Short visible status for the last action (stashed, stash full, nothing
   *  to restore, attachments pending). Cleared by the next edit. */
  notice: string
  /** `ok` for a confirmation, `warn` for a refusal. */
  noticeTone: 'ok' | 'warn'
  /** Move the draft onto the stack and clear the composer. */
  stash: () => void
  /** Put the latest entry into the composer. A draft already there is stashed
   *  in its place, so nothing is lost. */
  restoreLatest: () => void
  onKeyDown: (e: React.KeyboardEvent) => void
}

/**
 * A no-op press (nothing typed, nothing stashed) must still never open the
 * browser's "Save page" dialog from the composer, yet a save handler that is
 * active at the same time (a side-panel editor listening on `document`, which
 * skips an event that is already `defaultPrevented`) must still get it. So the
 * default is cancelled at the last stop of the bubble, `window`, after every
 * document-level handler has run. The listener is one-shot and is dropped on
 * the next task if the event never reached it (a handler stopped it).
 */
function suppressSaveDialogAfterOtherHandlers(): void {
  const cancel = (ev: Event) => ev.preventDefault()
  window.addEventListener('keydown', cancel, { once: true })
  setTimeout(() => window.removeEventListener('keydown', cancel), 0)
}

export function usePromptStash({
  slotKey,
  value,
  blocks,
  onChange,
  onBlocksChange,
  onReseed,
  inert = false,
  attachmentCount = 0,
  chordLabel,
  touch = false,
}: UsePromptStashOptions): PromptStashState {
  // A restored entry remains the safe copy until this composer changes the
  // restored snapshot (edit, send/clear, or stash again). It is hidden only
  // from this hook's count and restore candidates. Slot changes and unmounts
  // deliberately abandon this marker without deleting storage: at worst the
  // user sees a restored draft that is also still stashed, never a lost one.
  const pendingConsumeRef = useRef<{
    slot: string
    id: string
    value: string
    blocks: PasteBlock[]
  } | null>(null)
  /** The stored count for `slot` minus the pending restored copy. Every count
   *  that is re-read from storage goes through here, so no site can forget
   *  the filter and show the restored draft as stashed again. */
  const countStored = useCallback((slot: string) => {
    const pendingId = pendingConsumeRef.current?.slot === slot
      ? pendingConsumeRef.current.id
      : null
    return loadPromptStash(slot).filter(entry => entry.id !== pendingId).length
  }, [])
  const [count, setCount] = useState(() => (slotKey ? countStored(slotKey) : 0))
  // The live region announces a DOM change, not a value. A refusal repeated
  // with the same text (two chord presses at the cap) would otherwise be a
  // no-op state update that React bails out of, leaving the text node as it
  // was and the screen reader silent the second time. Each announcement
  // carries a sequence number the region uses as a `key`, so the text node
  // is replaced even when the words are the same.
  const [live, setLive] = useState({ text: '', seq: 0 })
  const setAnnouncement = useCallback((text: string) => {
    // Clearing an already-clear region is still a no-op: it announces
    // nothing and must not cost a render (the slot effect clears on mount).
    setLive(prev => (text === '' && prev.text === '' ? prev : { text, seq: prev.seq + 1 }))
  }, [])
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [noticeTone, setNoticeTone] = useState<'ok' | 'warn'>('warn')
  // The draft the current notice was shown against. A notice goes stale on the
  // next edit, but the edit a stash itself makes (clearing the composer) must
  // not wipe the "Draft stashed" confirmation, so the baseline is the draft as
  // the action leaves it, not as it found it.
  const noticeBaseline = useRef({ value, blocks: blocks.length })
  const mounted = useRef(false)
  const currentSlot = useRef(slotKey)
  const currentDraft = useRef({ value, blocks })
  currentSlot.current = slotKey
  currentDraft.current = { value, blocks }

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  const showNotice = useCallback((text: string, after = { value, blocks: blocks.length }, tone: 'ok' | 'warn' = 'warn') => {
    noticeBaseline.current = after
    setNoticeTone(tone)
    setNotice(text)
  }, [blocks.length, value])

  useEffect(() => {
    if (value !== noticeBaseline.current.value || blocks.length !== noticeBaseline.current.blocks) {
      noticeBaseline.current = { value, blocks: blocks.length }
      setNotice('')
      setError('')
    }
  }, [value, blocks.length])

  useEffect(() => {
    pendingConsumeRef.current = null
    setCount(slotKey ? countStored(slotKey) : 0)
    setAnnouncement('')
    setError('')
    setNotice('')
  }, [countStored, setAnnouncement, slotKey])

  // Another tab writing any entry for this slot changes the count shown here.
  useEffect(() => {
    if (!slotKey) return
    const prefix = promptStashKey(slotKey)
    const onStorage = (e: StorageEvent) => {
      if (e.key === null || e.key.startsWith(prefix)) {
        setCount(countStored(slotKey))
      }
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [countStored, slotKey])

  useEffect(() => {
    const pending = pendingConsumeRef.current
    if (!pending || pending.slot !== slotKey) return
    if (value === pending.value && JSON.stringify(blocks) === JSON.stringify(pending.blocks)) return

    // Clear first so repeated renders cannot queue duplicate removals. The
    // entry stays intact if this composer disappears or changes slot before
    // the locked callback runs.
    pendingConsumeRef.current = null
    const operationSlot = pending.slot
    void withPromptStashLock(() => {
      if (!mounted.current || currentSlot.current !== operationSlot) return
      removeStashEntry(operationSlot, pending.id)
      setCount(countStored(operationSlot))
    })
  }, [blocks, countStored, slotKey, value])

  const refuseForAttachments = useCallback(() => {
    setAnnouncement(touch
      ? i18nT('components.promptStash.attachments_touch')
      : i18nT('components.promptStash.attachments', { chord: chordLabel }))
    showNotice(i18nT('components.promptStash.notice_attachments'))
  }, [chordLabel, setAnnouncement, showNotice, touch])

  const stillCurrent = useCallback((operationSlot: string, operationValue: string, operationBlocks: PasteBlock[]) => (
    mounted.current
    && currentSlot.current === operationSlot
    && currentDraft.current.value === operationValue
    && currentDraft.current.blocks === operationBlocks
  ), [])

  /** Re-read, decide and mutate under the slot lock. The keyboard handler may
   *  already have read the stack to claim the chord synchronously. */
  const stashFrom = useCallback(() => {
    if (!slotKey) return
    const operationSlot = slotKey
    const operationValue = value
    const operationBlocks = blocks
    void withPromptStashLock((locked) => {
      if (!stillCurrent(operationSlot, operationValue, operationBlocks)) return
      const pendingId = pendingConsumeRef.current?.slot === operationSlot
        ? pendingConsumeRef.current.id
        : null
      const storedList = loadPromptStash(operationSlot)
      const list = storedList.filter(entry => entry.id !== pendingId)
      if (list.length >= PROMPT_STASH_MAX) {
        setCount(list.length)
        setAnnouncement(i18nT('components.promptStash.full', { max: PROMPT_STASH_MAX }))
        showNotice(i18nT('components.promptStash.notice_full', { max: PROMPT_STASH_MAX }))
        return
      }
      const written = makeStashEntry(operationValue, operationBlocks)
      // Size is checked here, not only inside `addStashEntry`, so the refusal
      // can say WHY (the stash is at its byte budget, not browser quota) and
      // what frees room (restore or send a stashed draft). The draft stays.
      if (!stashWriteFits(operationSlot, written)) {
        setCount(list.length)
        setError(i18nT('components.promptStash.too_large'))
        setAnnouncement('')
        setNotice('')
        return
      }
      if (!addStashEntry(operationSlot, written, locked)) {
        setCount(list.length)
        // The error renders as `role="alert"`, which announces on its own; a
        // copy in the polite live region would be spoken a second time.
        setError(i18nT('components.promptStash.save_failed'))
        setAnnouncement('')
        setNotice('')
        return
      }
      // Web Locks make the pre-write byte snapshot authoritative. Without
      // them, another tab can land a different slot between our check and
      // write. Keep that concurrent entry, roll back only ours, and leave this
      // draft in the composer with the size-specific refusal.
      if (!locked && promptStashBytes() > PROMPT_STASH_MAX_BYTES) {
        removeStashEntry(operationSlot, written.id)
        setCount(list.length)
        setError(i18nT('components.promptStash.too_large'))
        setAnnouncement('')
        setNotice('')
        return
      }
      // Browsers without Web Locks can interleave another add after this call's
      // read. Roll back only this write and retain the pending safe copy.
      const afterWrite = locked ? null : loadPromptStash(operationSlot)
      if (afterWrite && afterWrite.length > storedList.length + 1) {
        removeStashEntry(operationSlot, written.id)
        setCount(list.length)
        setAnnouncement(i18nT('components.promptStash.full', { max: PROMPT_STASH_MAX }))
        showNotice(i18nT('components.promptStash.notice_full', { max: PROMPT_STASH_MAX }))
        return
      }
      if (pendingId) {
        pendingConsumeRef.current = null
        removeStashEntry(operationSlot, pendingId)
      }
      const nextCount = list.length + 1
      setError('')
      setCount(nextCount)
      onChange('')
      if (operationBlocks.length) onBlocksChange?.([])
      onReseed?.('', [])
      setAnnouncement(touch
        ? i18nT('components.promptStash.stashed_touch', { count: nextCount })
        : i18nT('components.promptStash.stashed', { count: nextCount, chord: chordLabel }))
      showNotice(touch
        ? i18nT('components.promptStash.notice_stashed_touch')
        : i18nT('components.promptStash.notice_stashed', { chord: chordLabel }), { value: '', blocks: 0 }, 'ok')
    })
  }, [blocks, chordLabel, onBlocksChange, onChange, onReseed, setAnnouncement, showNotice, slotKey, stillCurrent, touch, value])

  const stash = useCallback(() => {
    if (!slotKey || inert) return
    if (attachmentCount > 0) { refuseForAttachments(); return }
    if (isDraftEmpty(value, blocks)) return
    // Read the stack from storage rather than trusting state: another composer
    // bound to the same slot may have changed it.
    stashFrom()
  }, [attachmentCount, blocks, inert, refuseForAttachments, slotKey, stashFrom, value])

  const restoreLatest = useCallback(() => {
    if (!slotKey || inert) return
    if (attachmentCount > 0) { refuseForAttachments(); return }
    const operationSlot = slotKey
    const operationValue = value
    const operationBlocks = blocks
    void withPromptStashLock((locked) => {
      if (!stillCurrent(operationSlot, operationValue, operationBlocks)) return
      const pendingId = pendingConsumeRef.current?.slot === operationSlot
        ? pendingConsumeRef.current.id
        : null
      const storedList = loadPromptStash(operationSlot)
      const list = storedList.filter(entry => entry.id !== pendingId)
      const latest = list[list.length - 1]
      if (!latest) {
        setCount(0)
        const emptyText = touch
          ? i18nT('components.promptStash.empty_touch')
          : i18nT('components.promptStash.empty', { chord: chordLabel })
        setAnnouncement(emptyText)
        // Neutral tone: an empty press is not a mistake, and the visible line
        // teaches the gesture instead of just saying nothing happened.
        showNotice(emptyText, undefined, 'ok')
        return
      }
      const current = isDraftEmpty(operationValue, operationBlocks)
        ? null
        : makeStashEntry(operationValue, operationBlocks)
      // A swap preserves the visible draft before removing the restored copy.
      // A draft too large for the byte budget refuses the swap the same way a
      // full stack would: the composer keeps it, and nothing is restored over it.
      if (current && !stashWriteFits(operationSlot, current)) {
        setCount(list.length)
        setError(i18nT('components.promptStash.too_large'))
        setAnnouncement('')
        setNotice('')
        return
      }
      if (current && !addStashEntry(operationSlot, current, locked)) {
        setCount(countStored(operationSlot))
        setError(i18nT('components.promptStash.save_failed'))
        setAnnouncement('')
        return
      }
      // Mirror the unlocked stash check before touching the restored copy.
      if (current && !locked && promptStashBytes() > PROMPT_STASH_MAX_BYTES) {
        removeStashEntry(operationSlot, current.id)
        setCount(countStored(operationSlot))
        setError(i18nT('components.promptStash.too_large'))
        setAnnouncement('')
        setNotice('')
        return
      }
      // In the no-lock fallback, another tab can add after this call's read.
      // Refuse before removing the restored copy, and roll back only our draft.
      if (current && !locked && loadPromptStash(operationSlot).length > storedList.length + 1) {
        removeStashEntry(operationSlot, current.id)
        setCount(countStored(operationSlot))
        setAnnouncement(i18nT('components.promptStash.full', { max: PROMPT_STASH_MAX }))
        showNotice(i18nT('components.promptStash.notice_full', { max: PROMPT_STASH_MAX }))
        return
      }
      if (pendingId) removeStashEntry(operationSlot, pendingId)
      // Retaining the restored entry is the normal safe-copy behaviour. At the
      // cap, though, the composer and its own draft persistence now protect
      // that text, so consume the restored copy rather than leave an eleventh
      // independently stored entry for other tabs to count.
      const retainRestored = !current || loadPromptStash(operationSlot).length <= PROMPT_STASH_MAX
      if (!retainRestored) removeStashEntry(operationSlot, latest.id)
      const left = current ? list.length : list.length - 1
      pendingConsumeRef.current = retainRestored
        ? {
            slot: operationSlot,
            id: latest.id,
            value: latest.text,
            blocks: latest.blocks,
          }
        : null
      setError('')
      setCount(left)
      // Text first, then blocks: the tokens in the text are what keep the blocks
      // from being pruned as orphans.
      onChange(latest.text)
      if (latest.blocks.length || operationBlocks.length) onBlocksChange?.(latest.blocks)
      onReseed?.(latest.text, latest.blocks)
      // Restoring the last draft says only that it came back, not "0 drafts".
      const restoredText = left === 0
        ? i18nT('components.promptStash.restored_last')
        : i18nT('components.promptStash.restored', { count: left })
      setAnnouncement(current ? i18nT('components.promptStash.swapped', { count: left }) : restoredText)
      if (current) {
        showNotice(i18nT('components.promptStash.notice_swapped'), { value: latest.text, blocks: latest.blocks.length }, 'ok')
      } else {
        // A plain restore fills an empty input with old text; say so on screen,
        // not only in the live region.
        showNotice(restoredText, { value: latest.text, blocks: latest.blocks.length }, 'ok')
      }
    })
  }, [attachmentCount, blocks, chordLabel, countStored, inert, onBlocksChange, onChange, onReseed, refuseForAttachments, setAnnouncement, showNotice, slotKey, stillCurrent, touch, value])

  const onKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (!slotKey || inert || e.defaultPrevented || !isStashChord(e.nativeEvent)) return
    // A held key auto-repeats. Each repeat sees the draft the previous one
    // left (cleared after a stash, filled after a restore), so acting on it
    // would flip stash/restore for as long as the key is down and land the
    // draft wherever the release happened. A repeat is claimed exactly as the
    // press it repeats would be (no browser save dialog while the key is held)
    // but performs nothing; the one action is the first press's.
    const repeat = e.nativeEvent.repeat
    if (attachmentCount > 0) {
      // A refusal is still ours: the user meant the stash, not the browser.
      e.preventDefault()
      e.stopPropagation()
      if (!repeat) refuseForAttachments()
      return
    }
    const list = loadPromptStash(slotKey)
    const plan = planStashChord(isDraftEmpty(value, blocks), list.length)
    // A press that stashes, restores or is refused at the cap is ours alone:
    // no browser save dialog, no document-level save.
    if (plan !== 'none') {
      e.preventDefault()
      e.stopPropagation()
    } else {
      suppressSaveDialogAfterOtherHandlers()
    }
    if (repeat) return
    if (plan === 'none' || plan === 'restore') {
      restoreLatest()
      return
    }
    stashFrom()
  }, [attachmentCount, blocks, inert, refuseForAttachments, restoreLatest, slotKey, stashFrom, value])

  const full = count >= PROMPT_STASH_MAX
  const draftEmpty = isDraftEmpty(value, blocks)
  const stashDisabledReason: PromptStashDisabledReason = attachmentCount > 0
    ? 'attachments'
    : full
      ? 'full'
      : draftEmpty
        ? 'empty'
        : null
  return {
    count,
    full,
    canStash: !inert && !draftEmpty && attachmentCount === 0 && !full,
    stashDisabledReason,
    canRestore: !inert && count > 0 && attachmentCount === 0,
    restoreSwaps: !inert && count > 0 && attachmentCount === 0 && !draftEmpty,
    announcement: live.text,
    announcementKey: live.seq,
    error,
    notice,
    noticeTone,
    stash,
    restoreLatest,
    onKeyDown,
  }
}
