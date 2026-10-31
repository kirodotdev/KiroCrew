import { useRef, useCallback, type Dispatch, type MutableRefObject, type SetStateAction } from 'react'
import type { MoveSlotOptions } from '../../../hooks/useMoveSlotToFolder'
import { store, type AppDispatch } from '../../../store'
import type { ChatSlot } from '../../../types'
import { type FollowupItem, setPendingInput, dismissFollowupItem, clearFolderSuggestion } from '../../../store/chatSlice'
import { api } from '../../../api/client'
import { mergeIntoDraft } from '../../../utils/chatDrafts'
import { i18nT } from '../../../i18n/t'

interface FollowupCardActionsOptions {
  dispatch: AppDispatch
  moveSlotToFolder: (slotKey: string, folderId: string | null, opts?: MoveSlotOptions) => void
  activeSlot: string | null
  folderSuggestion: { folderId: string; folderName: string; breadcrumb: string; ts: number; turns: number; } | undefined
  followupTsBySlot: Record<string, { items: FollowupItem[]; ts: number; }>
  inputRef: MutableRefObject<string>
}

/**
 * The follow-up and folder-suggestion cards' in-session actions: add a follow-up
 * to this session, accept or decline a folder suggestion, and name the branch a
 * follow-up's worktree starts on.
 *
 * Called from ChatPage at the position its statements held, so React registers its effects in the same order.
 */
export function useFollowupCardActions({
  dispatch, moveSlotToFolder, activeSlot, folderSuggestion, followupTsBySlot, inputRef,
}: FollowupCardActionsOptions) {
  // ── Follow-up card actions (suggest_followup MCP tool) ───────────────────
  // "Add to this session" PRE-FILLS this composer and sends nothing (the Start
  // routes, in useFollowupStarts, send). `setPendingInput` is consumed by
  // usePendingInputIntake (called from ChatPage), which drops the text into the
  // composer and flags the prefill hint — the same path the Projects page and
  // command palette use, so there is one prefill mechanism, not a parallel one.
  //
  // Live per-slot card timestamps, read inside async actions without making them
  // depend on (and re-create on) every card change.
  const followupTsRef = useRef<Record<string, { items: FollowupItem[]; ts: number }>>({})
  followupTsRef.current = followupTsBySlot
  const followupAddToSession = useCallback((item: FollowupItem, index: number) => {
    if (!activeSlot) return
    // APPEND when the composer already holds unsent text: the pending-input path
    // replaces the draft and persists it, so a plain set would silently destroy
    // whatever the user was mid-way through typing. `inputRef` is the live
    // composer value; `mergeIntoDraft` is shared with the error → agent hand-off
    // drain so the two paths cannot drift.
    dispatch(setPendingInput(mergeIntoDraft(inputRef.current, item.prompt)))
    // Remove only this item, as Start and Skip do: the other suggestions are
    // still worth offering. The RENDERED card's ts guards a newer card for this
    // slot that landed between render and click, which this click never saw.
    dispatch(dismissFollowupItem({ slot: activeSlot, index, ts: followupTsRef.current[activeSlot]?.ts, item }))
  }, [dispatch, activeSlot, inputRef])

  // Folder suggestion: accepting reuses the ONE move path every other surface
  // (row menu, drag-to-folder, new-chat-in-folder) already funnels through, so
  // the optimistic update and its guarded rollback are inherited rather than
  // re-implemented here. Both answers clear the card by the ts it rendered with,
  // for the same reason the follow-up actions do. The card passes the folder id
  // its dropdown currently shows — the suggestion by default, or whatever the
  // user picked instead.
  const folderSuggestionAccept = useCallback((folderId: string) => {
    if (!activeSlot || !folderSuggestion) return
    moveSlotToFolder(activeSlot, folderId)
    dispatch(clearFolderSuggestion({ slot: activeSlot, ts: folderSuggestion.ts }))
  }, [activeSlot, folderSuggestion, moveSlotToFolder, dispatch])

  const folderSuggestionDecline = useCallback(() => {
    if (!activeSlot || !folderSuggestion) return
    // Nothing to tell the backend: it already spent its one offer for this slot,
    // so declining is purely "take the card away".
    dispatch(clearFolderSuggestion({ slot: activeSlot, ts: folderSuggestion.ts }))
  }, [activeSlot, folderSuggestion, dispatch])

  // Fallback branch name when the agent did not supply one: slugify the title
  // under FOLLOWUP_BRANCH_RE's grammar (the server re-validates, so a slug that
  // degenerates to empty is replaced rather than sent and rejected).
  const followupBranchFor = useCallback((item: FollowupItem) => {
    if (item.branch) return item.branch
    const slug = item.title
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '')
      .slice(0, 40)
    return `followup/${slug || 'suggestion'}`
  }, [])
  return {
    followupTsRef, followupAddToSession, folderSuggestionAccept, folderSuggestionDecline, followupBranchFor,
  }
}

// Follow-up starts in flight, keyed by origin slot, card ts and prompt. Module
// scope, because the card unmounts on a session switch and ChatPage unmounts on
// a route change while the card itself lives on in Redux: a click on a remounted
// card must not run the same prompt in a second session.
const pendingFollowupStarts = new Set<string>()

interface FollowupStartsOptions {
  activeSlot: string | null
  currentSlot: ChatSlot | undefined
  followupTsRef: MutableRefObject<Record<string, { items: FollowupItem[]; ts: number; }>>
  followupBranchFor: (item: FollowupItem) => string
  openFollowupSession: (item: FollowupItem, target: { project?: string; folderId?: string | null; originSlot: string | null; originTs: number | undefined; originIndex: number; startedOn: string | null; startedMoves: number; fail: { notCreated: string; noSession: string; notSent: string; unconfirmed: string; notOpened: string; notFocused: string; }; }) => Promise<void>
  setActionError: Dispatch<SetStateAction<{ title?: string; message: string; preserveOnSwitch?: boolean } | null>>
}

/**
 * The follow-up card's two "start elsewhere" routes (a new worktree, a new chat),
 * built on the page's openFollowupSession.
 *
 * Called from ChatPage at the position its statements held, so React registers its effects in the same order.
 */
export function useFollowupStarts({
  activeSlot, currentSlot, followupTsRef, followupBranchFor, openFollowupSession, setActionError,
}: FollowupStartsOptions) {
  // Both spawn routes file the new session in the spawning session's sidebar
  // folder, so a follow-up started from a session filed under a project folder
  // lands in that same folder instead of at the sidebar's top level.
  // `currentSlot` IS the spawning session, and its `folder_id` is undefined
  // when that session is itself unfiled — in which case the new session lands
  // top-level. No default folder is invented; unfiled stays unfiled. The card's
  // ts is captured up front so completion clears only THIS card: a newer card
  // can arrive for the same slot while the request is in flight, and the older
  // action's completion must not clobber it.
  const followupOrigin = useCallback((index: number) => ({
    originIndex: index,
    originSlot: activeSlot,
    originTs: activeSlot ? followupTsRef.current[activeSlot]?.ts : undefined,
    // The committed active slot at the click, read before the route's first
    // await (the worktree create), so a session the user opens meanwhile is
    // seen as their choice and not overridden by the finished start.
    startedOn: store.getState().chat.activeSlot,
    // The user-move counter at the click: leaving and coming back to the same
    // session still counts as having chosen where to be.
    startedMoves: store.getState().chat.activeSlotMoves ?? 0,
    folderId: currentSlot?.folder_id,
  }), [activeSlot, currentSlot?.folder_id, followupTsRef])

  const runFollowupStart = useCallback(async (
    item: FollowupItem,
    origin: ReturnType<typeof followupOrigin>,
    start: () => Promise<void>,
  ) => {
    const key = JSON.stringify([origin.originSlot, origin.originTs, item.prompt])
    if (pendingFollowupStarts.has(key)) return
    pendingFollowupStarts.add(key)
    try {
      await start()
    } catch (error) {
      const chat = store.getState().chat
      const leftOrigin = chat.activeSlot !== origin.startedOn || (chat.activeSlotMoves ?? 0) !== origin.startedMoves
      // The card that issued this start lost its inline error if the user left
      // the origin at any point, even if they came back (it unmounted), or if the
      // origin card was replaced or removed meanwhile (a new card resets errors).
      const originCardGone = !!origin.originSlot && followupTsRef.current[origin.originSlot]?.ts !== origin.originTs
      if (origin.originSlot && (leftOrigin || originCardGone)) {
        const message = error instanceof Error ? error.message : String(error)
        setActionError({ message, preserveOnSwitch: true })
      }
      throw error
    } finally {
      pendingFollowupStarts.delete(key)
    }
  }, [followupTsRef, setActionError])

  const followupStartInWorktree = useCallback(async (item: FollowupItem, index: number) => {
    const origin = followupOrigin(index)
    await runFollowupStart(item, origin, async () => {
      const repo = currentSlot?.project
      if (!repo) throw new Error(i18nT('pages.chatPage.this_session_has_no_project_directory_to_branch'))
      // Create the worktree FIRST: if git refuses (branch exists, not a repo),
      // we must not have already spawned an empty session the user has to clean
      // up. The card surfaces the thrown message inline.
      const res = await api.createWorktree(repo, followupBranchFor(item))
      const path = res?.path
      if (!path) throw new Error(res?.error || i18nT('pages.chatPage.worktree_creation_returned_no_path'))
      // The worktree exists from here on, so every failure names its path: the
      // create endpoint is idempotent for its own destination, so pressing the
      // button again reuses this worktree instead of refusing it. The not-opened
      // and not-focused messages arrive after the prompt was sent, so they point
      // at the sidebar instead of a retry.
      await openFollowupSession(item, {
        ...origin,
        project: path,
        fail: {
          notCreated: i18nT('pages.chatPage.followup_worktree_session_not_created', { path }),
          noSession: i18nT('pages.chatPage.followup_worktree_no_session_returned', { path }),
          notSent: i18nT('pages.chatPage.followup_worktree_prompt_not_sent', { path }),
          unconfirmed: i18nT('pages.chatPage.followup_prompt_unconfirmed'),
          notOpened: i18nT('pages.chatPage.followup_worktree_session_not_opened', { path }),
          notFocused: i18nT('pages.chatPage.followup_worktree_session_not_focused', { path }),
        },
      })
    })
  }, [currentSlot?.project, followupBranchFor, followupOrigin, openFollowupSession, runFollowupStart])

  // The route for a session that has no repository to branch: a plain new chat
  // in the spawning session's project directory (where a new chat starts by
  // default when it has none), with no git call at all. Pressing the button
  // again after a failure opens another chat. The not-opened and not-focused
  // messages arrive after the prompt was sent, so they point at the sidebar
  // instead of a retry.
  const followupStartInNewChat = useCallback(async (item: FollowupItem, index: number) => {
    const origin = followupOrigin(index)
    await runFollowupStart(item, origin, () => openFollowupSession(item, {
      ...origin,
      project: currentSlot?.project || undefined,
      fail: {
        notCreated: i18nT('pages.chatPage.followup_chat_not_created'),
        noSession: i18nT('pages.chatPage.followup_chat_not_created'),
        notSent: i18nT('pages.chatPage.followup_chat_prompt_not_sent'),
        unconfirmed: i18nT('pages.chatPage.followup_prompt_unconfirmed'),
        notOpened: i18nT('pages.chatPage.followup_chat_not_focused'),
        notFocused: i18nT('pages.chatPage.followup_chat_not_focused'),
      },
    }))
  }, [currentSlot?.project, followupOrigin, openFollowupSession, runFollowupStart])
  return {
    followupStartInWorktree, followupStartInNewChat,
  }
}
