import { unwrapResult } from '@reduxjs/toolkit'

import { api } from '../../api/client'
import { i18nT } from '../../i18n/t'
import { createChatForView, handOffUnstagedPrefill } from '../../lib/createdChatView'
import type { AppDispatch, RootState } from '../../store'
import { captureForkOrigin, createSlot, forkMayOpen, switchSlot } from '../../store/chatSlice'
import type { ChatSlot } from '../../types'
import { writePrefill } from '../../utils/navIntent'

/** The source session's settings the new session carries. */
export type InitialPromptSource = Pick<ChatSlot, 'agent' | 'agent_kind' | 'project' | 'folder_id'> | undefined

/**
 * "Draft a new session from this prompt" (#25977): open a fresh session on the
 * source session's agent, project and sidebar folder, with `prompt` in its
 * composer, unsent. Resolves `true` once the new session took the view (the
 * host then closes its menu), `false` when the session was created but the
 * user had moved to another session meanwhile: it stays listed, and nothing is
 * put in any composer. Throws when no session was created.
 */
export async function startFromInitialPrompt(
  dispatch: AppDispatch,
  getState: () => RootState,
  { prompt, source, mode }: { prompt: string; source: InitialPromptSource; mode?: string },
): Promise<boolean> {
  const agent = source?.agent || undefined
  const agentKind = agent ? (source?.agent_kind || undefined) : undefined
  const project = source?.project || undefined
  if (!project) {
    // No project to carry: one create names the agent, through the shared
    // create-and-show path. The prompt rides it as `prefill`, which the slot
    // restore delivers once to the new session's composer, merged with
    // anything typed while the create ran.
    const view = await createChatForView(dispatch, getState, {
      agent, agent_kind: agentKind, mode, folder_id: source?.folder_id, prefill: prompt,
    })
    if (!view.shown) return false
    handOffUnstagedPrefill(dispatch, view.slot.key, prompt, view.prefillStaged)
    return true
  }
  // A project to carry. The create resolves an agent with no project scope, so
  // an agent defined only in that project's `.kiro/agents` would be refused
  // there. Create the session scoped to the project first, NOT yet shown
  // (`activate: false`, as the follow-up card's routes do), then pick the
  // agent: the agent route resolves it inside the session's project. That
  // route moves a non-project agent's session to its own default directory,
  // so the project is applied once more after it. Any failure removes the
  // half-made session, so nothing is left behind and no composer is seeded.
  //
  // `activate: false` also skips the create reducer's own `forkMayOpen` check,
  // so this path makes it itself, and a session the user opened during these
  // round-trips keeps the view. The origin is taken right after the create's
  // synchronous `pending`, which numbers this request: that number orders it
  // after a fork or create clicked earlier, so such a request finishing later
  // cannot take the view back from this one.
  //
  // The prompt is NOT staged on the create: a staged prefill lives 30s, and
  // the agent and project round-trips that follow can outlast it. It is staged
  // below, once the session is set up and allowed to open, right before the
  // switch whose restore reads it.
  const creating = dispatch(createSlot({
    mode, project, folder_id: source?.folder_id, activate: false,
  }))
  const origin = captureForkOrigin(getState())
  const created = unwrapResult(await creating)
  const key = created?.key
  if (!key) throw new Error(i18nT('pages.chat.initialPrompt.new_session_failed_detail'))
  if (agent) {
    try {
      await api.chatSlotAgent(key, agent, agentKind)
      await api.chatSlotProject(key, project)
    } catch (err) {
      await api.deleteChatSlot(key).catch(() => {})
      throw err
    }
  }
  if (!forkMayOpen(getState(), origin)) return false
  const staged = writePrefill(key, prompt)
  // Just created above, so a 404 here is a create/fetch race on a session
  // that exists: keep the selection on it.
  await dispatch(switchSlot({ key, keepTargetOnMissing: true, requestSeq: origin.seq })).unwrap()
  // The staged prefill is read by the restore on this switch; the in-memory
  // hand-off is only the fallback for a refused storage write, and only into
  // the session it is for.
  if (getState().chat.activeSlot !== key) return false
  handOffUnstagedPrefill(dispatch, key, prompt, staged)
  return true
}
