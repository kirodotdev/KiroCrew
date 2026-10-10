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
  otherSlotsStashKeys,
  planStashChord,
  promptStashBytes,
  promptStashKey,
  removePromptStashKeys,
  removeStashEntry,
  stashWriteFits,
  storedEntryBytes,
  withPromptStashLock,
} from '../utils/promptStash'

/** Paste blocks compared by content: a host that defaults its prop to `[]`
 *  hands a fresh array on every render, so identity alone would treat any
 *  re-render (such as the + menu closing) as a changed draft. */
function sameBlocks(a: PasteBlock[], b: PasteBlock[]): boolean {
  return a === b || (a.length === b.length && JSON.stringify(a) === JSON.stringify(b))
}

export interface UsePromptStashOptions {
  /** Chat slot the stack belongs to. Falsy disables the feature. */
  slotKey: string | null | undefined
  value: string
  blocks: PasteBlock[]
  /** Receives the hook's programmatic writes (clear on stash, fill on restore).
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

/** What a host's send reports: a promise of whether the server confirmed
 *  delivery, or nothing when the host cannot tell. Only `true` consumes a
 *  restored stash entry; `false`, a rejection and no answer all keep it. */
export type PromptStashSendResult = void | Promise<boolean>

export interface PromptStashState {
  count: number
  full: boolean
  /** `stash` would act: non-empty draft, no attachments, stack not full. */
  canStash: boolean
  /** Why the menu's stash row is disabled, when the draft state explains it. */
  stashDisabledReason: PromptStashDisabledReason
  /** `restoreLatest` would act: something stashed, no attachments, empty composer. */
  canRestore: boolean
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
  /** Put the latest entry into an empty composer. */
  restoreLatest: () => void
  /** Run the host's composer send. A restored draft's entry is removed only
   *  when the returned promise resolves `true` (confirmed delivery). */
  trackSend: (send: () => PromptStashSendResult) => void
  /** Entries other sessions hold, offered for clearing after a refusal at the
   *  byte budget. 0 when there is no such offer. */
  reclaimCount: number
  /** Delete exactly the entries counted in `reclaimCount`. The status line
   *  calls it only after the user confirms. */
  clearOtherSessions: () => void
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

/** Drop a reclaim offer without a re-render when there is none. */
const clearedReclaim = (prev: string[]): string[] => (prev.length === 0 ? prev : [])

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
  // A restored entry remains the safe copy until the restored draft is
  // confirmed delivered or stashed again; edits keep it, and emptying the
  // composer hands it back to the stash. It is hidden only
  // from this hook's count and restore candidates. Slot changes and unmounts
  // deliberately abandon this marker without deleting storage: at worst the
  // user sees a restored draft that is also still stashed, never a lost one.
  const pendingConsumeRef = useRef<{
    slot: string
    id: string
    value: string
    blocks: PasteBlock[]
  } | null>(null)
  /** Entries whose restored draft is in a send that has not settled yet. Hidden
   *  like the pending copy, never deleted until the host confirms delivery. */
  const inFlightRef = useRef(new Set<string>())
  /** The stored list for `slot` minus the pending restored copy and any copy
   *  in flight. Every count and candidate list that is re-read from storage
   *  goes through here, so no site can forget the filter and show the
   *  restored draft as stashed again. */
  const visibleStored = useCallback((slot: string) => {
    const pendingId = pendingConsumeRef.current?.slot === slot
      ? pendingConsumeRef.current.id
      : null
    return loadPromptStash(slot).filter(entry => entry.id !== pendingId && !inFlightRef.current.has(entry.id))
  }, [])
  const countStored = useCallback((slot: string) => visibleStored(slot).length, [visibleStored])
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
  // Keys listed when a stash was refused at the byte budget. The clear removes
  // only these, so a draft another tab stashes after the offer is kept.
  const [reclaimKeys, setReclaimKeys] = useState<string[]>([])
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
      setReclaimKeys(clearedReclaim)
    }
  }, [value, blocks.length])

  useEffect(() => {
    pendingConsumeRef.current = null
    setCount(slotKey ? countStored(slotKey) : 0)
    setAnnouncement('')
    setError('')
    setNotice('')
    setReclaimKeys(clearedReclaim)
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
    // An edit keeps the stash entry. The composer's own draft save is debounced
    // and can fail on quota (or never run if the tab dies), so until the draft
    // is safely elsewhere the entry is its only durable copy. An emptied
    // composer is not proof the draft was used: a send path that bypasses
    // `trackSend` (voice auto-submit) clears it optimistically too. So an
    // empty composer only hands the entry back to the stash, where it shows
    // in the count again; nothing is deleted here. Only a confirmed delivery
    // (`trackSend`) or stashing it again (`pendingId`) removes the entry.
    if (value !== '' || blocks.length > 0) return
    pendingConsumeRef.current = null
    setCount(countStored(pending.slot))
  }, [blocks, countStored, slotKey, value])

  const refuseForAttachments = useCallback(() => {
    const message = i18nT('components.promptStash.notice_attachments')
    setAnnouncement(message)
    showNotice(message)
  }, [setAnnouncement, showNotice])

  // Runs inside the stash lock, so the key list is read synchronously.
  const refuseTooLarge = useCallback((slot: string, count: number) => {
    const message = i18nT('components.promptStash.too_large')
    setCount(count)
    setError('')
    setReclaimKeys(otherSlotsStashKeys(slot))
    setAnnouncement(message)
    showNotice(message)
  }, [setAnnouncement, showNotice])

  const clearOtherSessions = useCallback(() => {
    const keys = reclaimKeys
    if (keys.length === 0) return
    setReclaimKeys(clearedReclaim)
    void removePromptStashKeys(keys).then(() => {
      if (!mounted.current) return
      const message = i18nT('components.promptStash.cleared_others', { count: keys.length })
      setAnnouncement(message)
      showNotice(message, undefined, 'ok')
    })
  }, [reclaimKeys, setAnnouncement, showNotice])

  const stillCurrent = useCallback((operationSlot: string, operationValue: string, operationBlocks: PasteBlock[]) => (
    mounted.current
    && currentSlot.current === operationSlot
    && currentDraft.current.value === operationValue
    && sameBlocks(currentDraft.current.blocks, operationBlocks)
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
      // The cap counts copies in flight: they still occupy storage, and only
      // the pending copy is released by this write.
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
      if (!stashWriteFits(operationSlot, written, { releasing: pendingId })) {
        refuseTooLarge(operationSlot, list.length)
        return
      }
      if (!addStashEntry(operationSlot, written, { releasing: pendingId })) {
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
      // The pending restored copy is removed just below, so it does not count.
      const releasedBytes = pendingId ? storedEntryBytes(operationSlot, pendingId) : 0
      if (!locked && promptStashBytes() - releasedBytes > PROMPT_STASH_MAX_BYTES) {
        removeStashEntry(operationSlot, written.id)
        refuseTooLarge(operationSlot, list.length)
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
      // Re-read rather than `list.length + 1`: a copy in flight is stored but
      // not shown.
      const nextCount = countStored(operationSlot)
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
  }, [blocks, chordLabel, countStored, onBlocksChange, onChange, onReseed, refuseTooLarge, setAnnouncement, showNotice, slotKey, stillCurrent, touch, value])

  const stash = useCallback(() => {
    if (!slotKey || inert) return
    if (attachmentCount > 0) { refuseForAttachments(); return }
    if (isDraftEmpty(value, blocks)) return
    // Read the stack from storage rather than trusting state: another composer
    // bound to the same slot may have changed it.
    stashFrom()
  }, [attachmentCount, blocks, inert, refuseForAttachments, slotKey, stashFrom, value])

  const restoreLatest = useCallback(() => {
    if (!slotKey || inert || !isDraftEmpty(value, blocks)) return
    if (attachmentCount > 0) { refuseForAttachments(); return }
    const operationSlot = slotKey
    const operationValue = value
    const operationBlocks = blocks
    void withPromptStashLock(() => {
      if (!stillCurrent(operationSlot, operationValue, operationBlocks)) return
      const list = visibleStored(operationSlot)
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
      const left = list.length - 1
      pendingConsumeRef.current = {
        slot: operationSlot,
        id: latest.id,
        value: latest.text,
        blocks: latest.blocks,
      }
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
      setAnnouncement(restoredText)
      // A plain restore fills an empty input with old text; say so on screen,
      // not only in the live region.
      showNotice(restoredText, { value: latest.text, blocks: latest.blocks.length }, 'ok')
    })
  }, [attachmentCount, blocks, chordLabel, inert, onBlocksChange, onChange, onReseed, refuseForAttachments, setAnnouncement, showNotice, slotKey, stillCurrent, touch, value, visibleStored])

  const trackSend = useCallback((send: () => PromptStashSendResult) => {
    const pending = pendingConsumeRef.current
    const claim = pending && pending.slot === currentSlot.current ? pending : null
    // Off the restored marker BEFORE the host clears the composer, so that
    // clear is not read as the user discarding the draft. Hidden while the
    // send is unresolved; the host's composer clear is optimistic, not proof.
    if (claim) {
      pendingConsumeRef.current = null
      inFlightRef.current.add(claim.id)
    }
    const result = send()
    if (!claim) return
    const settle = (delivered: boolean) => {
      inFlightRef.current.delete(claim.id)
      void withPromptStashLock(() => {
        // Storage is slot-keyed, so a confirmed delivery is removed even after
        // an unmount or slot switch. Anything else leaves the entry stored and
        // shown again: a failed send's text may exist only in pane state.
        if (delivered) removeStashEntry(claim.slot, claim.id)
        if (mounted.current && currentSlot.current === claim.slot) setCount(countStored(claim.slot))
      })
    }
    const verdict = result as Promise<boolean> | undefined
    if (verdict && typeof verdict.then === 'function') {
      verdict.then(ok => settle(ok === true), () => settle(false))
    } else {
      // No signal from this host: never assume delivery.
      settle(false)
    }
  }, [countStored])

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
    canRestore: !inert && draftEmpty && count > 0 && attachmentCount === 0,
    announcement: live.text,
    announcementKey: live.seq,
    error,
    notice,
    noticeTone,
    stash,
    restoreLatest,
    trackSend,
    reclaimCount: reclaimKeys.length,
    clearOtherSessions,
    onKeyDown,
  }
}
