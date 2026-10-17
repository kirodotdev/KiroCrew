import { useState, useRef, useCallback, useLayoutEffect, useMemo } from 'react'
import { takePaneDraft, takePaneDraftSwap, writePaneDraft, mergePaneDraft, subscribePaneDraft } from '../utils/chatPaneDrafts'
import { type PasteBlock, mergeCarriedDraft } from '../utils/pasteTokens'
import { useComposerPastes } from '../chat-core/composer/composerPastes'
import { parseDirTokens } from '../utils/fileTokens'
import { useComposerArrivals } from '../utils/composerSendHolds'

/**
 * `ChatPane`'s composer draft: the typed text, the staged files and folder
 * tokens, and the collapsed paste blocks, all pane-LOCAL. It parks the draft
 * per slot when the pane is rebound or unmounted and restores it on return,
 * takes late arrivals (recoveries, upload results) for the slot on screen, and
 * hands a refused payload back (`restoreIntoComposer`) into the live composer
 * or into the sending slot's parked draft. The refs it returns are what the
 * pane's send, upload and queue paths read after an await.
 */

export function useChatPaneDraft(slotKey: string) {
  const [input, setInput] = useState('')
  const [pendingFiles, setPendingFiles] = useState<string[]>([])
  const pendingDirs = useMemo(() => parseDirTokens(input).map(t => t.rel), [input])
  // Collapsed paste blocks behind the `[ Paste #N · M lines ]` tokens in
  // `input`: the Paste atom, carried to ChatInput by the Composer root in ChatPane.tsx
  // (without it a large paste stays raw text). Pane-local like `input`, parked
  // and restored WITH it (chatPaneDrafts), expanded at send, cleared with it.
  // `read()` is the latest list ahead of a render, `install` swaps it on a
  // rebind, and `carry` brings a refused payload's blocks back renumbered past
  // the held ones -- synchronously, so two recoveries in one React batch
  // compose instead of the second dropping the first's blocks.
  const composerPastes = useComposerPastes()
  const { blocks: pasteBlocks, set: setPasteBlocks, read: readPastes, install: installPastes, carry: carryIntoComposer } = composerPastes
  // The composer is pane-LOCAL state, and a host may rebind one pane instance
  // to another slot without remounting it (the Members page mounts
  // `<ChatPane slotKey={activeSlot}>` with no key). Two things must then hold:
  // the text typed for member A must not ride along into B's composer, and a
  // recovery that lands AFTER the switch (a refused or unconfirmed send is in
  // flight for seconds) must go to A, not to whatever is on screen now. Both
  // are served by the pane's per-slot draft store (utils/chatPaneDrafts — the
  // repo's slot-draft-store mechanism, on the pane's own keys): on a slot
  // change the outgoing slot's composer is parked and the incoming slot's is
  // restored; a late recovery for a slot that is no longer shown merges into
  // that slot's parked draft. The store outlives the pane, so leaving the page
  // mid-flight loses nothing either.
  const slotKeyRef = useRef(slotKey)
  const inputRef = useRef(input)
  const pendingFilesRef = useRef(pendingFiles)
  // False once the pane is gone: a recovery or upload result that lands after
  // unmount has no composer to write to (a setState on an unmounted component
  // is a silent no-op), so it goes to the store instead.
  const mountedRef = useRef(false)
  inputRef.current = input
  pendingFilesRef.current = pendingFiles
  // A LAYOUT effect, not a passive one: `slotKeyRef` and the park/take below
  // must move in the same commit as the `slotKey` prop. With a passive effect
  // there is a gap between the commit and the effect in which the ref still
  // names the OLD slot, so a recovery resolving in that gap (a refusal for
  // the slot just left) reads `forSlot === slotKeyRef.current`, writes into
  // the live composer, and is then overwritten when this effect parks the
  // stale input and installs the incoming slot's draft — the refused text
  // would be lost. Rebinding during commit closes that window.
  useLayoutEffect(() => {
    mountedRef.current = true
    const prev = slotKeyRef.current
    // Take, not read: once a slot is live in this composer, the composer is
    // the one copy — the store entry is cleared so a later park cannot
    // overwrite an arrival that came in between.
    if (prev !== slotKey) {
      writePaneDraft(prev, { text: inputRef.current, files: pendingFilesRef.current, pastes: readPastes() })
      slotKeyRef.current = slotKey
      const incoming = takePaneDraft(slotKey)
      setInput(incoming.text)
      setPendingFiles(incoming.files)
      // Installed, not merely set (see `composerPastes` above): a recovery for the
      // incoming slot that lands in this same commit carries on top of ITS
      // parked blocks, not the outgoing slot's.
      installPastes(incoming.pastes)
    } else {
      // First mount: pick up whatever this slot parked before (a page the user
      // left mid-draft, a recovery that landed while the pane was gone). The
      // parked blocks come in with their text, re-numbered past any the
      // composer already holds so no two tokens share a number.
      const parked = takePaneDraft(slotKey)
      if (parked.text) {
        const carried = carryIntoComposer(parked.text, parked.pastes)
        setInput(cur => mergeCarriedDraft(cur, carried))
      }
      if (parked.files.length) setPendingFiles(cur => [...cur, ...parked.files.filter(f => !cur.includes(f))])
    }
    // While this slot is on screen, a late arrival for it (a recovery or upload
    // result from a PREVIOUS pane instance that showed the same slot, then
    // unmounted) is handed straight to the live composer. Without this it would
    // sit in the store until this pane's own park overwrote it.
    const unsubscribe = subscribePaneDraft(slotKey, () => {
      const arrived = takePaneDraft(slotKey)
      // A suggested prompt replacing the one it put here before, while the
      // composer still holds that one untouched (`swapPaneDraftText`).
      const swap = takePaneDraftSwap(slotKey)
      if (arrived.text) {
        const carried = carryIntoComposer(arrived.text, arrived.pastes)
        setInput(cur => (swap !== undefined && cur.trim() === swap ? carried.text : mergeCarriedDraft(cur, carried)))
      }
      if (arrived.files.length) setPendingFiles(cur => [...cur, ...arrived.files.filter(f => !cur.includes(f))])
    })
    // Unmount (or the next rebind, which runs this cleanup first): park the
    // live composer so nothing typed or recovered is lost with the instance.
    return () => {
      unsubscribe()
      mountedRef.current = false
      writePaneDraft(slotKeyRef.current, { text: inputRef.current, files: pendingFilesRef.current, pastes: readPastes() })
    }
  }, [slotKey, carryIntoComposer, readPastes, installPastes])
  useComposerArrivals(slotKey, (paths, arrivalSlot) => {
    if (arrivalSlot !== slotKeyRef.current) return false
    setPendingFiles(previous => [
      ...previous,
      ...paths.filter(path => !previous.includes(path)),
    ])
    return true
  })
  /** Put a payload the server never accepted back into the composer.
   *
   *  APPEND, never replace and never DROP: a send is in flight for seconds and
   *  the user can type a fresh message in that window, so neither payload may
   *  overwrite the other — preferring the newer one silently discards the message
   *  the error row is telling them to retry, preferring the older one loses work
   *  they just did. `mergeRecoveredDraft` owns that rule for every recovery site
   *  in the app, this pane's two included (a failed `doSend` and a failed
   *  question-card fallback); attachments merge here as a set union so a file
   *  re-picked mid-flight is not double-attached.
   *
   *  `forSlot` is the slot the payload was typed into. When the pane has since
   *  been rebound to another slot — or unmounted altogether — the text goes
   *  into THAT slot's parked draft (shown again when the user returns to it)
   *  instead of into the composer the user is now looking at, which belongs to
   *  someone else's conversation, or into a component that no longer exists. */
  const restoreIntoComposer = useCallback((text: string, files: string[] = [], pastes: PasteBlock[] = [], forSlot: string = slotKeyRef.current) => {
    if (!mountedRef.current || forSlot !== slotKeyRef.current) { mergePaneDraft(forSlot, text, files, pastes); return }
    // The paste blocks behind the payload's tokens come back with it, numbered
    // past whatever the composer holds now, or the restored token would be a
    // chip with nothing behind it.
    const carried = carryIntoComposer(text, pastes)
    setInput(prev => mergeCarriedDraft(prev, carried))
    if (files.length) setPendingFiles(prev => [...prev, ...files.filter(f => !prev.includes(f))])
  }, [carryIntoComposer])

  return {
    input, setInput, pendingFiles, setPendingFiles, pendingDirs, composerPastes, pasteBlocks, setPasteBlocks,
    readPastes, carryIntoComposer, slotKeyRef, inputRef, pendingFilesRef, mountedRef, restoreIntoComposer,
  }
}
