import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { X } from 'lucide-react'
import { useAppDispatch, useAppStore } from '../store'
import { setAgentSwitchNotice } from '../store/chatSlice'
import { createChatForView, showCreatedChat } from '../lib/createdChatView'
import type { AgentSwitchNotice } from '../utils/agentSwitchFeedback'
import { errMessage } from '../utils/thunkError'
import ErrorNotice from './ErrorNotice'
import { useGuardedLeave, useIsCurrentUrl, useMayLeaveForNavigation, useNavigationStakeNow } from './NavigationLeaveGuard'
import { i18nT } from '../i18n/t'

/**
 * The shared agent-switch notice. When the refused pick was a crewmate that can
 * only start a new chat, the notice offers that new chat: a session created on
 * the crewmate, filed like the New button's, and opened through the shared
 * create-and-show helpers, so a user who moved elsewhere while the create ran
 * stays where they went. The chat the pick was made in keeps its agent and
 * history either way. A failed create stays in the banner as an ErrorNotice
 * and the button stays for a retry. A notice marked `error` (a refused switch,
 * including the new-chat offer, or a failed setting write) renders its message
 * as an ErrorNotice with the agent hand-off; any other notice is plain status
 * text.
 */
export default function AgentSwitchNoticeBanner({ notice }: { notice: AgentSwitchNotice }) {
  const dispatch = useAppDispatch()
  const store = useAppStore()
  const navigate = useNavigate()
  const guardedLeave = useGuardedLeave()
  const mayLeave = useMayLeaveForNavigation()
  const stakeNow = useNavigationStakeNow()
  const isCurrentUrl = useIsCurrentUrl()
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)
  const agent = notice.newChatAgent
  // The banner element survives a notice swap, so a create error from the
  // replaced offer would otherwise stay under the new notice.
  useEffect(() => { setCreateError(null) }, [notice])
  const openNewChat = async (leaveGranted: boolean) => {
    if (!agent || creating) return
    // `setAgentSwitchNotice` stores a fresh object every time, so identity
    // tells this offer apart from a later notice for the same crewmate (the
    // same pick refused again in another chat while this create ran).
    const offered = notice
    const offerStillShown = () => store.getState().chat.agentSwitchNotice === offered
    setCreating(true)
    setCreateError(null)
    try {
      const view = await createChatForView(dispatch, store.getState, { agent, agent_kind: 'member', defaultFolder: true })
      // The chat exists now, so the offer is spent even when the user moved
      // away and the new chat is not the one on screen. A notice posted while
      // the create ran (the create's own "session folder unavailable" line,
      // another refused switch) replaced the offer and is left for the user.
      // That holds when the page below refuses the navigate too.
      if (offerStillShown()) dispatch(setAgentSwitchNotice(null))
      // A click that found work at stake already asked, and the user agreed to
      // discard it: asking again repeats the same question on a page whose
      // guard does not clear its draft on accept, and a "stay" then would hide
      // the chat that was just made. `leaveGranted` tells a landing page that
      // acts on the mark not to ask either. A click that found nothing at
      // stake asked nobody, but the user can start editing while the create
      // runs, so ask the live guard; a refusal keeps the new chat and leaves
      // the user where they are.
      showCreatedChat(view, route => {
        if (leaveGranted) navigate(route, { state: { leaveGranted: true } })
        else if (mayLeave()) navigate(route)
      })
    } catch (err) {
      // `unwrap()` rejects with a serialized plain object; errMessage reads it.
      // A failure that settles after the offer was replaced has no offer to
      // attach to, so it does not overlay the newer notice.
      if (offerStillShown()) setCreateError(errMessage(err) || i18nT('components.errorBoundary.something_went_wrong'))
    } finally {
      setCreating(false)
    }
  }
  return (
    <div role={notice.error ? undefined : 'status'} data-testid="agent-switch-notice" className="fixed z-[70] top-safe-offset-14 left-safe-offset-4 right-safe-offset-4 sm:left-auto sm:w-[440px] bg-bg-elevated border rounded-lg p-3 flex items-start gap-3 shadow-xl animate-rise" style={{ borderColor: 'color-mix(in srgb, var(--warn) 45%, transparent)' }}>
      {/* Wraps so the new-chat button moves under the text on a narrow screen
          instead of crushing it; the dismiss X keeps its own column. */}
      <div className="flex-1 min-w-0 flex flex-wrap items-center gap-x-3 gap-y-2">
        {notice.error ? (
          <ErrorNotice
            askAgent
            testId="agent-switch-notice-error"
            className="basis-full"
            message={notice.message}
          />
        ) : (
          <span className="text-sm text-text flex-1 min-w-[10rem]">{notice.message}</span>
        )}
        {agent && (
          <button
            type="button"
            // The banner is shell-mounted, so this click can come from a page
            // holding a draft. Ask before the create, not just the navigate:
            // a kept draft must not leave a new chat behind. `/chat` is the
            // only route the action navigates to, so a click already there
            // skips the ask. The stake is read before the ask so the delayed
            // navigate knows whether this click asked the user (see above).
            onClick={() => {
              const atStake = !isCurrentUrl('/chat') && stakeNow()
              guardedLeave(() => openNewChat(atStake), '/chat')
            }}
            disabled={creating}
            data-testid="agent-switch-notice-new-chat"
            className="max-w-full break-words text-left text-[12px] font-medium px-2 py-1 rounded-md border border-border text-text hover:bg-bg-hover disabled:opacity-60 cursor-pointer"
          >
            {i18nT('utils.agentSwitchFeedback.new_chat_with', { name: agent })}
          </button>
        )}
        <ErrorNotice
          askAgent
          testId="agent-switch-notice-create-error"
          className="basis-full"
          message={createError}
        />
      </div>
      <button onClick={() => dispatch(setAgentSwitchNotice(null))} aria-label={i18nT('app.dismiss')} className="shrink-0 text-muted hover:text-text leading-none p-0.5"><X className="lucide-inline w-4 h-4" /></button>
    </div>
  )
}
