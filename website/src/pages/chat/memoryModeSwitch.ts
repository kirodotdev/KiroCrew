/**
 * The memory-mode switch: replace the current session with one in a new
 * memory mode, carrying the unsent draft and any running uploads across.
 *
 * Loaded on demand (ChatPage imports it when the user picks a mode), because
 * it runs only on that one rare click and the page's main chunk is at its size
 * budget. The caller holds the composer lock for the whole call, so nothing in
 * either session can change while this runs.
 */
import { captureForkOrigin, createSlot, deleteSlot, forkMayOpen, switchSlot } from '../../store/chatSlice'
import type { AppDispatch, AppStore } from '../../store'
import { handOverComposerUploads, landComposerAttachments, takeComposerArrivals } from '../../utils/composerSendHolds'
import { errMessage } from '../../utils/thunkError'
import { i18nT } from '../../i18n/t'
import type { MemoryMode } from '../../components/MemoryModeChip'

type Ref<T> = { current: T }

export interface MemorySwitchContext {
  dispatch: AppDispatch
  store: AppStore
  /** The session being replaced, as listed. */
  slot: {
    agent?: string | null
    agent_kind?: 'member' | 'template' | ''
    model?: string | null
    folder_id?: string | null
    color_index?: number | null
    color_hex?: string | null
    project?: string | null
  } | undefined
  mode: string | undefined
  composerMountedRef: Ref<boolean>
  composerSlotRef: Ref<string | null>
  inputRef: Ref<string>
  pendingFilesRef: Ref<string[]>
  pasteBlocksRef: Ref<object[]>
  pendingSessionsRef: Ref<object[]>
  drafts: Ref<Record<string, string>>
  fileDrafts: Ref<Record<string, string[]>>
  pasteDrafts: Ref<Record<string, object[]>>
  sessionRefDrafts: Ref<Record<string, object[]>>
  pickedFileTokens: Ref<Record<string, object | undefined>>
  saveDrafts: () => void
  showActionError: (message: string) => void
  setActionError: (error: { message: string; preserveOnSwitch: true }) => void
}

/**
 * Create the replacement, carry the draft into it and switch to it. Returns
 * whether the old slot handed everything over and may be deleted.
 */
export async function runMemorySwitch(
  c: MemorySwitchContext,
  from: string,
  newMode: MemoryMode,
  attempt: { timedOut: boolean },
): Promise<boolean> {
  const unknownError = () => i18nT('pages.chatPage.unknown_error')
  // Create-first-then-delete: deleting the active slot first
  // would make deleteSlot jump focus to a sibling. Creating and
  // switching first makes the new slot active, so the delete skips the
  // sibling navigation. Carry agent/project/folder/color so
  // the recreated slot keeps its identity and placement.
  const old = c.slot
  const opts = {
    agent: old?.agent || undefined,
    agent_kind: old?.agent ? old?.agent_kind || undefined : undefined,
    model: old?.model || undefined,
    mode: c.mode,
    memory_mode: newMode,
    folder_id: old?.folder_id ?? null,
    color_index: old?.color_index ?? null,
    color_hex: old?.color_hex ?? null,
    project: old?.project ?? null,
  }
  // Created in the background so the unsent draft can move to the new slot
  // BEFORE it activates: the slot-change effect restores the incoming slot's
  // draft, which for a fresh slot is empty, and the old slot (where the
  // draft lives) is deleted afterwards.
  let created: { key?: string } | undefined
  // The pending action numbers this create (`requestSeq`) synchronously, so
  // the origin read right after it carries the create's own number.
  const creating = c.dispatch(createSlot({ ...opts, activate: false }))
  const origin = captureForkOrigin(c.store.getState())
  try { created = await creating.unwrap() } catch (error) {
    c.showActionError(errMessage(error) || unknownError())
    return false
  }
  const next = created?.key
  if (!next) return false
  // The lock timed out, or the page unmounted while the create ran (its
  // composer refs are detached and may not match what the user now has):
  // the composer may have changed since, so keep both sessions untouched.
  if (attempt.timedOut || !c.composerMountedRef.current) return false
  const activeNow = () => c.store.getState().chat.activeSlot
  // An aborted switch keeps the old session, so the replacement would be a
  // stray empty one. Drop it -- unless the user is now looking at it, where
  // deleting it would yank them to a sibling.
  const dropReplacement = (report: boolean) => {
    if (activeNow() === next) return
    void c.dispatch(deleteSlot(next)).unwrap().catch(error => {
      if (report) c.showActionError(errMessage(error) || unknownError())
    })
  }
  // The user moved to another session while the create was in flight, or
  // went to one and came back: the switch is abandoned, by the rule every
  // create follows (`forkMayOpen`). Switching to `next` would undo where
  // they went, and deleting the old slot would destroy the draft it still
  // holds. Drop the unused replacement instead. The switch also requires
  // that the active slot and the composer still show `from`, because it
  // carries the composer of `from` and deletes `from`: an automatic move off
  // `from`, which `forkMayOpen` allows, leaves no composer of `from` to carry.
  if (!forkMayOpen(c.store.getState(), origin) || activeNow() !== from || c.composerSlotRef.current !== from) {
    dropReplacement(true)
    return false
  }
  // Read the live composer, not the stored draft: the debounced draft save
  // may not have caught the last keystrokes yet.
  if (c.inputRef.current) c.drafts.current[next] = c.inputRef.current
  const carriedFiles = c.pendingFilesRef.current.slice()
  if (carriedFiles.length) c.fileDrafts.current[next] = carriedFiles.slice()
  if (c.pasteBlocksRef.current.length) c.pasteDrafts.current[next] = c.pasteBlocksRef.current.map(b => ({ ...b }))
  if (c.pendingSessionsRef.current.length) c.sessionRefDrafts.current[next] = c.pendingSessionsRef.current.map(r => ({ ...r }))
  const tokens = c.pickedFileTokens.current[from]
  if (tokens) c.pickedFileTokens.current[next] = { ...tokens }
  c.saveDrafts()
  // Watch for the user going back to the old session while the switch loads:
  // once they have used it again it is no longer ours to delete, even if they
  // moved on again before the switch resolved.
  let left = false
  let revisited = false
  const unsubscribe = c.store.subscribe(() => {
    const now = activeNow()
    if (now !== from) left = true
    else if (left) revisited = true
  })
  try {
    await c.dispatch(switchSlot({ key: next, requestSeq: origin.seq })).unwrap()
  } catch (error) {
    // The rejected switch moves the view back to the old slot, and the
    // slot-change effect clears any error not marked to survive a switch --
    // which would erase the only report that the switch failed.
    c.setActionError({ message: errMessage(error) || unknownError(), preserveOnSwitch: true })
    // Silent: a cleanup failure must not replace the switch failure, and a
    // replacement that 404ed has nothing left to delete. After a timeout or
    // an unmount the replacement may hold the user's work, so it is kept,
    // as on the success path below.
    if (!attempt.timedOut && c.composerMountedRef.current) dropReplacement(false)
    return false
  } finally {
    unsubscribe()
  }
  // Timed out or unmounted while the switch loaded: the old slot's draft
  // and uploads may have moved on since. Keep both sessions.
  if (attempt.timedOut || !c.composerMountedRef.current) return false
  if (revisited || activeNow() === from) {
    dropReplacement(true)
    return false
  }
  // Each upload still running from the old slot moves to the replacement
  // with its hold (see uploadFiles). One that already finished while the
  // switch loaded landed in the old slot's draft after the carry above -- or
  // waits undrained in its arrivals -- so move it across too, otherwise it
  // is deleted with the old slot.
  handOverComposerUploads(from, next)
  const late = [...(c.fileDrafts.current[from] ?? []), ...takeComposerArrivals(from)]
    .filter((p, i, all) => !carriedFiles.includes(p) && all.indexOf(p) === i)
  // Through the arrival registry, which persists until a composer showing
  // `next` drains it.
  landComposerAttachments(next, late)
  return true
}
