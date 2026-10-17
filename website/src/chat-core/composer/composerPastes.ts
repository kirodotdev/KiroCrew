/**
 * The Paste atom (chat-core RFC §3 layer 4, P3-c): the collapsed paste blocks
 * behind the composer text's `[ Paste #N · M lines ]` tokens.
 *
 * A large paste becomes a token in the text and a block here. The editor
 * (`ChatInput`) adds and removes blocks as the user pastes, deletes a chip or
 * expands one; it reaches them through the `Composer` root, which carries the
 * host's {@link ComposerPasteSlice} (`<Composer pastes>`), so a host mounting
 * the root threads no paste props. Around a submit three things happen to the
 * blocks, each owned here or by the turn rather than by the host:
 *
 * - expand-on-send: the submit hands `blocks` to `buildOutgoingTurn`, which
 *   expands the live ones on the wire and returns the ones it expanded;
 * - bubble store: {@link storeSentPastes} records a send's blocks so history
 *   load can re-collapse the server's expanded echo into the same chips;
 * - carry-back: a refused submit's blocks come back through `carry`,
 *   renumbered past the blocks the composer holds now, so no two tokens share
 *   a number (the panes; the main chat still carries its blocks in ChatPage).
 *
 * `read()` is the latest block list without waiting for a render: `install`
 * and `carry` advance it synchronously, so two carries landing in one React
 * batch compose (the second carries on top of the first instead of erasing
 * its blocks while both tokens reach the text). `set` is the plain state
 * setter the editor's own edits go through, and `read()` catches up with it
 * on the next render.
 *
 * State only, no effects: a host can call the hook wherever its state sat
 * without moving any effect in its order. Where blocks persist per slot stays
 * the host's (each keeps its drafts differently). No Redux.
 */
import { useCallback, useMemo, useRef, useState, type Dispatch, type SetStateAction } from 'react'

import { carryPastes, saveStoredPaste, type CarriedPastes, type PasteBlock } from '../../utils/pasteTokens'
import type { StoredPasteEntry } from './outgoingTurn'

/** What the `Composer` root carries to the editor. */
export interface ComposerPasteSlice {
  readonly blocks: PasteBlock[]
  set: (next: PasteBlock[]) => void
}

export interface ComposerPastes extends ComposerPasteSlice {
  /** The blocks as of this render. */
  readonly blocks: PasteBlock[]
  /** Same contract as a `useState` setter; setting the current list is a no-op. */
  set: Dispatch<SetStateAction<PasteBlock[]>>
  /** The latest block list, ahead of the next render after `install` / `carry`. */
  read: () => PasteBlock[]
  /** Replace the blocks now (a rebind restoring another slot's draft). */
  install: (next: PasteBlock[]) => void
  /** Bring a refused payload's blocks back into the composer: `carryPastes`
   *  against `read()`, installed before it returns. The result's text is the
   *  payload with any renumbered tokens rewritten, for the caller's text merge
   *  (`mergeCarriedDraft`). With no blocks to carry nothing is written, so a
   *  plain `set` still pending in the same batch keeps its value. */
  carry: (text: string, sent: readonly PasteBlock[]) => CarriedPastes
}

/** The composer's paste blocks. Member functions are stable; the object
 *  changes identity only when `blocks` does. */
export function useComposerPastes(): ComposerPastes {
  const [blocks, set] = useState<PasteBlock[]>([])
  const latest = useRef(blocks)
  latest.current = blocks
  const read = useCallback(() => latest.current, [])
  const install = useCallback((next: PasteBlock[]) => {
    latest.current = next
    set(next)
  }, [])
  const carry = useCallback((text: string, sent: readonly PasteBlock[]): CarriedPastes => {
    const carried = carryPastes(text, sent as PasteBlock[], latest.current)
    if (sent.length) install(carried.pastes)
    return carried
  }, [install])
  return useMemo(() => ({ blocks, set, read, install, carry }), [blocks, read, install, carry])
}

/** Record a send's blocks in the paste side table (`saveStoredPaste`); a turn
 *  that expanded no paste carries no entry and writes nothing. */
export function storeSentPastes(entry: StoredPasteEntry | undefined): void {
  if (entry) saveStoredPaste(entry.expanded, entry.display, entry.pastes, entry.files)
}
