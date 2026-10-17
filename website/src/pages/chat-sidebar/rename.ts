/** Inline rename for session rows and folder headers, and the Radix menu focus
 *  plumbing that keeps a rename opened from a menu from being cancelled by the menu's
 *  own focus restore. */
import { useState, useRef, useCallback, useEffect, type MutableRefObject } from 'react'
import { measureAutoGrowTextarea, useAutoGrowTextarea } from '../../hooks/useAutoGrowTextarea'
import { sseSlotTitle } from '../../store/dashboardSlice'
import { api } from '../../api/client'
import { generateSlotTitle } from '../../hooks/slotTitleGeneration'
import { errMessage } from '../../utils/thunkError'
import { i18nT } from '../../i18n/t'
import type { AppDispatch, RootState } from '../../store'
import type { Store } from '@reduxjs/toolkit'
import type { QueryClient } from '@tanstack/react-query'

/** Max height (px) of the inline session-rename <textarea> before it scrolls.
 *  ~6 lines at the row's `ROW_TITLE_CLS` type. Shared by the auto-grow hook
 *  (grows while typing) and the open effect (sizes on every open). */
const RENAME_MAX_H = 120

/** Session-row rename state, its refused-rename recovery, and menu focus restore. */
export function useSessionRename({ dispatch, store, queryClient }: {
  dispatch: AppDispatch
  store: Store<RootState>
  queryClient: QueryClient
}) {
  const [renamingSlot, setRenamingSlot] = useState<string | null>(null)
  // In board view a multi-tag chat renders once per matching column, so
  // `renamingSlot === s.key` alone is true in every copy at once — the rename
  // input would mount in all columns and the shared ref would bind to the last.
  // renameScope pins the edit to the clicked render instance (the row's `scope`:
  // 'list' or the column id) so exactly one input mounts. Same idea as the
  // Framer layoutId `scope` note on `renderSessionRow`.
  const [renameScope, setRenameScope] = useState<string | null>(null)
  const [renameValue, setRenameValue] = useState('')
  // Set when the server refuses a rename; rendered through the sidebar-root
  // ErrorNotice cluster so the revert (below) never happens silently.
  const [renameError, setRenameError] = useState('')
  const cancelRenameRef = useRef(false)
  // Per-slot rename recovery state, keyed by slot. `gen` is a monotonic attempt
  // counter: a refused rename's delayed recovery may apply ONLY while its own
  // generation is still the latest (`rec.gen === myGen`). This defeats the
  // refuse-X -> rename-back-to-X-succeeds race, where a stale recovery of the
  // first attempt would otherwise restore the old server title over the newer
  // accepted one. `inflight` tracks the values still awaiting a server answer so
  // the entry is only dropped once the last one settles. Mirrors the proven
  // ChatPage inline-rename recovery (renameRecoveryRef there).
  const renameRecoveryRef = useRef(
    new Map<string, { baseline: string; inflight: Set<string>; gen: number }>()
  )
  const renameInputRef = useRef<HTMLTextAreaElement | null>(null)
  // The rename field is a wrapping, auto-growing <textarea> (not a single-line
  // <input>) so a long session title is fully visible while editing instead of
  // being clipped at the right edge — you can see and edit words that a
  // single-line box would scroll out of view. Enter still commits (bindEnter
  // preventDefaults it, so no newline is inserted). Caps at ~6 lines, then
  // scrolls. Only one row renames at a time (renamingSlot), so the single
  // shared ref always points at the one mounted textarea.
  useAutoGrowTextarea(renameInputRef, renameValue, RENAME_MAX_H)
  // Set by any menu's Rename item (session rows + folder headers) so the closing
  // menu's onCloseAutoFocus knows to skip Radix's trigger-focus-restore for this
  // one close (see the menu Content handlers in ChatSidebar). One-shot: read and cleared
  // on the next close.
  const suppressMenuRestoreRef = useRef(false)
  // ── Rename plumbing handed to the memoized rows ──────────────────────────
  // Stable identities (state setters + refs only), so arming a rename or
  // typing into it never invalidates other rows' props. The commit takes the
  // draft VALUE from the row as an argument rather than closing over
  // `renameValue` — a closure over it would mint a new handler per keystroke
  // and re-render every row on each key.
  const onRenameStart = useCallback((key: string, scope: string, title: string, fromMenu: boolean) => {
    if (fromMenu) suppressMenuRestoreRef.current = true
    setRenamingSlot(key)
    setRenameScope(scope)
    setRenameValue(title)
  }, [])
  const onRenameChange = useCallback((value: string) => {
    setRenameValue(value.replace(/[\r\n]+/g, ' '))
  }, [])
  const onRenameCancel = useCallback(() => {
    cancelRenameRef.current = true
    setRenamingSlot(null)
  }, [])
  const onRenameCommit = useCallback((key: string, value: string) => {
    if (!cancelRenameRef.current && value.trim()) {
      const refused = value.trim()
      // Take a generation for THIS attempt before any async work. A later
      // rename on the same slot bumps `rec.gen`, so a delayed recovery of an
      // earlier attempt sees `rec.gen !== myGen` and yields -- this is what
      // defeats the refuse-X -> rename-back-to-X race, where reverting the
      // first attempt's server title would otherwise stomp the newer accepted
      // one even though the store title equals `refused` in both.
      const rec = renameRecoveryRef.current.get(key) ?? { baseline: '', inflight: new Set<string>(), gen: 0 }
      rec.inflight.add(refused)
      rec.gen++
      const myGen = rec.gen
      renameRecoveryRef.current.set(key, rec)
      const settle = () => {
        rec.inflight.delete(refused)
        if (rec.inflight.size === 0 && rec.gen === myGen) renameRecoveryRef.current.delete(key)
      }
      dispatch(sseSlotTitle({ key, title: refused }))
      // Recovery on a refused rename must go through Redux: slot titles live in
      // the dashboard slice (written by `sseSlots` / `fetchSlots.fulfilled`),
      // and no React Query is registered on a plain ['chat-slots'] key, so an
      // invalidateQueries there is a no-op that leaves the optimistic
      // `sseSlotTitle` value on screen.
      //
      // Recover the ONE refused slot, not the whole list: `fetchSlots()` runs
      // `applySlots`, a whole-list replace that would overwrite a fresher
      // `sseSlotTitle` frame for ANY OTHER slot that arrived while the recovery
      // read was in flight (crash-data-loss anchor). We fetch the server list,
      // take only this slot's server title, and write it back via `sseSlotTitle`.
      //
      // The write is a compare-and-set gated on BOTH the generation and the
      // store title: recover only while this attempt is still the latest
      // (`rec.gen === myGen`) AND the store title is STILL the refused
      // optimistic value. If a newer rename bumped the generation, or an
      // authoritative frame (`sseSlots` / `sseSlotTitle`) changed this slot's
      // title, we yield to that newer truth instead of stomping it. Mirrors the
      // proven ChatPage inline-rename recovery.
      const mayRecover = () =>
        rec.gen === myGen &&
        store.getState().dashboard.slots.find(s => s.key === key)?.title === refused
      api.renameSlot(key, refused).then(() => settle(), async e => {
        setRenameError(errMessage(e) || i18nT('pages.chatPage.unknown_error'))
        try {
          const server = (await queryClient.fetchQuery({
            queryKey: ['chat-slots'], queryFn: () => api.chatSlots(), staleTime: 0, gcTime: 0,
          })).find((s: { key: string; title?: string }) => s.key === key)
          if (server?.title !== undefined && mayRecover()) {
            dispatch(sseSlotTitle({ key, title: server.title }))
          }
        } catch {
          // The recovery read itself failed (e.g. transport down). Leave the
          // optimistic title in place rather than guessing; the failure is
          // already surfaced via ErrorNotice, and the next authoritative frame
          // reconciles it. See the transport-failure note in the PR body.
        } finally {
          settle()
        }
      })
    }
    cancelRenameRef.current = false
    setRenamingSlot(null)
  }, [dispatch, queryClient, store])
  // Input modality tracker for menu-close focus handling: true while the most
  // recent interaction was a keyboard press. Capture-phase listeners so Radix's
  // own handlers can't reorder around us.
  const lastInputKeyboardRef = useRef(false)
  useEffect(() => {
    const onPointer = () => { lastInputKeyboardRef.current = false }
    const onKey = () => { lastInputKeyboardRef.current = true }
    document.addEventListener('pointerdown', onPointer, true)
    document.addEventListener('keydown', onKey, true)
    return () => {
      document.removeEventListener('pointerdown', onPointer, true)
      document.removeEventListener('keydown', onKey, true)
    }
  }, [])
  // The rename menus are Radix (ContextMenu/DropdownMenu). On close, Radix's
  // FocusScope restores focus to its trigger (the card) AFTER the input mounts.
  // That restore blurs the freshly-mounted input, firing its onBlur, which
  // cancels the edit before you can type — so the box flickers open and reverts.
  // The trigger-restore is suppressed on the rename path via onCloseAutoFocus
  // (below); this effect then focuses + selects the input on the next frame so
  // the caret lands ready to overtype (same rAF pattern as the new-chat textarea).
  // Keyed on both the slot AND its scope: a same-slot, scope-only change (retarget
  // the rename to a different column before the first column's blur-commit fires)
  // must re-run so focus lands in the newly-mounted column's input, not stay on
  // the old one. Re-running when only the scope changes is harmless (idempotent
  // focus+select). When the slot clears (commit/cancel/escape/blur), also clear
  // renameScope so no stale column identity lingers.
  useEffect(() => {
    if (!renamingSlot) { setRenameScope(null); return }
    const raf = requestAnimationFrame(() => {
      const el = renameInputRef.current
      if (el) {
        el.focus({ preventScroll: true }); el.select()
        // Size the box on OPEN too, not only when renameValue changes: after a
        // save, reopening the same slot sets renameValue to the identical title,
        // so useAutoGrowTextarea's value-keyed effect never fires and the freshly
        // mounted textarea would otherwise sit at its 1-line resting height and
        // clip a long name. Run the hook's own measure here so every open shows
        // the full name, border included.
        measureAutoGrowTextarea(el, RENAME_MAX_H)
      }
    })
    return () => cancelAnimationFrame(raf)
  }, [renamingSlot, renameScope])
  // Shared onCloseAutoFocus for every rename-hosting menu (session row context +
  // ⋯ dropdowns, and both folder-header ⋯ dropdowns). When Rename was the chosen
  // item it armed suppressMenuRestoreRef, so we preventDefault to stop Radix from
  // yanking focus back to the trigger — that restore would otherwise blur the
  // just-mounted rename input and cancel the edit. Every other item keeps the
  // default focus-restore intact.
  const onMenuCloseAutoFocus = useCallback((e: Event) => {
    if (suppressMenuRestoreRef.current) { suppressMenuRestoreRef.current = false; e.preventDefault(); return }
    // Pointer dismissals (outside click / mouse item pick) skip Radix's
    // focus-restore-to-trigger: the trigger lives inside a focus-within-revealed
    // hover group (folder headers AND session rows), so restoring focus pins
    // the action strip visible after the pointer has left the row. Keyboard
    // closes (Esc / Enter on an item) keep the restore — focus returning to
    // the trigger is exactly right for keyboard users (a11y).
    if (!lastInputKeyboardRef.current) e.preventDefault()
  }, [])
  return {
    renamingSlot, renameScope, renameValue, renameError, setRenameError, renameInputRef,
    suppressMenuRestoreRef, onRenameStart, onRenameChange, onRenameCancel, onRenameCommit,
    onMenuCloseAutoFocus,
  }
}

/** "Regenerate title" from a session row's menu: the same endpoint and store
 *  write as the chat header's hover-revealed button, reachable without making
 *  the session active. A refusal lands in `autoTitleError`, rendered by the
 *  sidebar-root ErrorNotice cluster beside the rename one; it names the session,
 *  because the notice sits away from the row that was picked. */
export function useSessionAutoTitle({ dispatch, store }: { dispatch: AppDispatch; store: Store<RootState> }) {
  const [autoTitleError, setAutoTitleError] = useState('')
  const onAutoTitle = useCallback((key: string) => {
    const run = generateSlotTitle(key, dispatch)
    if (!run) return
    setAutoTitleError('')
    run.catch(e => {
      const title = store.getState().dashboard.slots.find(s => s.key === key)?.title || key
      const reason = errMessage(e) || i18nT('pages.chatPage.unknown_error')
      setAutoTitleError(`${title}: ${reason}`)
    })
  }, [dispatch, store])
  return { autoTitleError, setAutoTitleError, onAutoTitle }
}

/** Folder rename state, scoped to the render instance being edited. */
export function useFolderRename({ renamingSlot, suppressMenuRestoreRef }: {
  renamingSlot: string | null
  suppressMenuRestoreRef: MutableRefObject<boolean>
}) {
  // Folder rename ref. See the focus effect below for why the rAF re-grab is needed.
  const folderEditInputRef = useRef<HTMLInputElement | null>(null)
  // Folder editing state
  const [editingId, setEditingId] = useState<string | null>(null)
  // Board view renders a folder once per column, so `editingId === folder.id`
  // is true in every column at once — the input would mount in all of them and
  // the shared ref would bind to the last. This scope pins the folder rename to
  // the clicked column's render instance (the columnId, or 'list' in list view)
  // so exactly one input mounts. renderFolderHeader passes 'list';
  // renderColumnFolder passes columnId. Folder CREATION needs no such scope —
  // it is a single root-level modal, not a per-column inline input.
  const [editScope, setEditScope] = useState<string | null>(null)
  const [editName, setEditName] = useState('')
  // Folder rename (renderFolderHeader + board renderColumnFolder) mounts its
  // input from a Radix menu, so plain autoFocus loses the same race as the
  // session rename: focus lands on the trigger/body after the menu tears down
  // (caret never in the box) and the default scroll-into-view yanks the
  // horizontally-scrolling board sideways. Re-grab focus on the next frame with
  // preventScroll so the board doesn't jump, selecting the text for overtype.
  // Keyed on both the id AND editScope: a same-id, scope-only change (retarget
  // to a different column before the first column's commit fires) must re-run so
  // focus lands in the newly-mounted column's input. The re-focus is idempotent
  // so re-running is harmless. When the id clears (commit/cancel/escape/blur),
  // clear the scope so no stale column identity lingers.
  useEffect(() => {
    if (!editingId) { setEditScope(null); return }
    const raf = requestAnimationFrame(() => {
      const el = folderEditInputRef.current
      if (el) { el.focus({ preventScroll: true }); el.select() }
    })
    return () => cancelAnimationFrame(raf)
  }, [editingId, editScope])
  // Belt-and-suspenders disarm of the one-shot suppress ref. It's normally
  // consumed by the very next onCloseAutoFocus, but if a menu is ever dismissed
  // without firing that (an outside-dismiss race), the ref would stay armed and
  // wrongly preventDefault the NEXT menu close. Whenever the sidebar is idle (no
  // edit open), force-disarm: no legitimate pending suppression can exist then.
  // Safe against the normal flow — during a live edit an id is non-null, so this
  // hasn't run yet; by the time all ids clear the real close already consumed it.
  useEffect(() => {
    if (!renamingSlot && !editingId) suppressMenuRestoreRef.current = false
  }, [renamingSlot, editingId, suppressMenuRestoreRef])
  return { folderEditInputRef, editingId, setEditingId, editScope, setEditScope, editName, setEditName }
}
