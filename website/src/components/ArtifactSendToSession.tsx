import { useEffect, useMemo, useRef, useState } from 'react'
import { Forward, Plus } from 'lucide-react'

import { useAppDispatch, useAppSelector } from '../store'
import { createSlot } from '../store/chatSlice'
import { DropdownMenuItem, DropdownMenuSeparator, DropdownMenuSub, DropdownMenuSubContent, DropdownMenuSubTrigger } from './ui/dropdown-menu'
import { prepareCurrentSlots, isEmptyNewSlot } from './commandPalette/providers/recentsProvider'
import { loadDrafts, mergeIntoDraft } from '../utils/chatDrafts'
import type { NavIntent } from '../utils/popoutController'
import type { ChatSlot } from '../types'
import { i18nT } from '../i18n/t'
import { errMessage } from '../utils/thunkError'
import { artifactReferencePrompt } from './artifactReference.prompt'

/** How many live sessions the menu lists. The menu is a quick hand-off, not a
 *  session browser — the sidebar and the command palette own the long tail. */
const MAX_LISTED_SESSIONS = 10


/**
 * Hand a reference to this artifact to a chat session's composer -- an existing
 * live session or a new one -- so the agent there can load it. The composer is
 * only PRE-FILLED (the user still presses send), and the reference is appended
 * to that session's stored draft rather than replacing it: the prefill consumer
 * overwrites the composer, so an unmerged seed would silently destroy text the
 * user had typed there.
 *
 * A hook rather than a self-contained menu because the trigger lives in the
 * toolbar's overflow menu, which closes (and unmounts its items) on select. The
 * "New session" create outlives that close, so its state is owned by the page.
 */
export function useArtifactSendToSession({ name, slug, active, onSend, beforeSend, onError }: {
  name: string
  slug: string
  /** False while the editor owns the surface. A create that settles after the
   *  user started editing is abandoned: navigating would drop the new edits. */
  active: boolean
  /** The page's navigation dispatcher, so a popout window forwards the
   *  hand-off to the main dashboard instead of navigating in place. */
  onSend: (intent: NavIntent) => void
  /** Runs the hand-off only once the page agrees to leave (e.g. the unsaved
   *  comment-draft prompt). It wraps session CREATION too, so cancelling the
   *  prompt never leaves an empty session nothing opens. `recheck` gates the
   *  navigation after a create on any draft STARTED since that prompt. */
  beforeSend: (proceed: (recheck: (go: () => void) => void) => void | Promise<void>) => void
  /** Failure text for the page's ErrorNotice stack, or `null` to clear it. */
  onError: (message: string | null) => void
}) {
  const dispatch = useAppDispatch()
  const slots = useAppSelector((s) => s.dashboard.slots)
  const [creating, setCreating] = useState(false)
  const liveRef = useRef(active)
  liveRef.current = active
  const mountedRef = useRef(true)
  useEffect(() => {
    mountedRef.current = true
    return () => { mountedRef.current = false }
  }, [])

  // Live sessions, most recent first. Sessions bound to an artifact are that
  // artifact's companion chat (this page's own lives behind the chat toggle),
  // and an empty placeholder is what "New session" already creates.
  const listed = useMemo(() => {
    const candidates = (slots ?? []).filter((s: ChatSlot) => !s.artifact && !isEmptyNewSlot(s))
    return prepareCurrentSlots(candidates).ordered.slice(0, MAX_LISTED_SESSIONS)
  }, [slots])

  const prompt = artifactReferencePrompt(name, slug)
  const handOff = (slotKey: string) => {
    const merged = mergeIntoDraft(loadDrafts()[slotKey], prompt)
    onSend({ path: '/chat', slotKey, prefill: { slotKey, prompt: merged, append: prompt } })
  }
  const handOffToNew = async (recheck: (go: () => void) => void) => {
    setCreating(true)
    onError(null)
    try {
      const slot = await dispatch(createSlot({ activate: false })).unwrap()
      if (!mountedRef.current || !liveRef.current) return
      // A comment started while the create was in flight is asked about
      // before leaving; the page skips drafts the first prompt already covered.
      recheck(() => handOff(slot.key))
    } catch (e) {
      if (mountedRef.current) onError(errMessage(e) || i18nT('components.errorBoundary.something_went_wrong'))
    } finally {
      if (mountedRef.current) setCreating(false)
    }
  }

  return {
    listed,
    creating,
    sendTo: (slotKey: string) => beforeSend(() => handOff(slotKey)),
    sendToNew: () => beforeSend(handOffToNew),
  }
}

export type ArtifactSendToSessionState = ReturnType<typeof useArtifactSendToSession>

/** The "Send to a session" submenu of the artifact toolbar's overflow menu. */
export function ArtifactSendToSessionSubmenu({ state }: { state: ArtifactSendToSessionState }) {
  return (
    <DropdownMenuSub>
      <DropdownMenuSubTrigger disabled={state.creating}>
        <Forward size={13} className="shrink-0 text-muted" aria-hidden="true" />
        <span>{i18nT('pages.artifactDetailPage.send_to_session')}</span>
      </DropdownMenuSubTrigger>
      <DropdownMenuSubContent className="min-w-[220px] max-w-[320px] max-h-[min(360px,var(--radix-dropdown-menu-content-available-height))]">
        <DropdownMenuItem onSelect={state.sendToNew}>
          <Plus size={13} aria-hidden="true" />
          {i18nT('pages.artifactDetailPage.send_to_new_session')}
        </DropdownMenuItem>
        {state.listed.length > 0 && <DropdownMenuSeparator />}
        {state.listed.map((s) => (
          <DropdownMenuItem key={s.key} onSelect={() => state.sendTo(s.key)}>
            <span className="truncate">{s.title || i18nT('pages.artifactDetailPage.untitled_session')}</span>
          </DropdownMenuItem>
        ))}
      </DropdownMenuSubContent>
    </DropdownMenuSub>
  )
}
