/** The live browser card in a crewmate's chat (Members page, member-mode slots).
 *
 *  A crewmate's chat draws no tool rows (`filterCrewmateChat`), so the turn
 *  grouping never sees the `browser` call that `TurnBlock` anchors its card on.
 *  This keeps that call as ONE row per turn, under its own role, and draws it as
 *  the same `BrowserLiveCard` the chat page mounts: the stream, the step bar,
 *  replay, takeover and the sign-in sheet. The card is the person's own view of
 *  the browser and the place they answer its asks, so it is not machinery. */
import BrowserLiveCard, { browserLiveAnchor, isBrowserToolMessage } from '../../pages/chat/BrowserLiveCard'
import { isTurnEnd, opensDisplayTurn } from '../../pages/chat/groupDisplayItems'
import type { MessageRenderer } from '../../app-sdk/messageRenderers'
import type { ChatMessage } from '../../types'
import { filterCrewmateChat, isCrewmateChatRow } from './crewmateBubbles'

/** The role a turn's first `browser` call is drawn under in a crewmate's chat. */
export const CREWMATE_BROWSER_ROLE = 'crewmate_browser_live'

/** A row that starts the next turn's card. A steer (`meta.steer`, whatever its
 *  role: a typed one or a wake steered in) belongs to the turn already running.
 *  So does a parked send: drawn as a bubble (`withQueuedAsBubbles`), or written
 *  at send time with `queuedEntryId` while the running turn goes on below it.
 *  The parked turn starts once the running one ends (`isTurnEnd`). */
function opensCardTurn(m: ChatMessage): boolean {
  return opensDisplayTurn(m) && !m.meta?.steer && !m.meta?.queuedBubble && !m.meta?.queuedEntryId
}

/** `filterCrewmateChat`, plus each turn's first `browser` call as a card row.
 *  Read over the unfiltered rows, so a patrol wake (a nudge or inject row the
 *  chat hides) still ends the turn before it: a card a later turn follows
 *  carries `cardTurnOver`, the "turn complete" `TurnBlock` reads from its turn. */
export function filterCrewmateChatWithBrowser(messages: ChatMessage[]): ChatMessage[] {
  if (!messages.some(isBrowserToolMessage)) return filterCrewmateChat(messages)
  const out: ChatMessage[] = []
  let card = -1 // where this turn's card row sits in `out`; -1 until it browses
  for (const m of messages) {
    if (opensCardTurn(m)) {
      if (card >= 0) out[card] = { ...out[card], meta: { ...out[card].meta, cardTurnOver: true } }
      card = -1
    }
    if (isBrowserToolMessage(m)) {
      if (card < 0) {
        card = out.length
        out.push({ ...m, role: CREWMATE_BROWSER_ROLE })
      }
    } else if (isCrewmateChatRow(m)) {
      out.push(m)
    }
    // shortcut: a turn that ends with no reply carries no stats, so a parked
    // turn after it joins its card; a dispatch marker on the row would fix it.
    if (isTurnEnd(m) && card >= 0) {
      out[card] = { ...out[card], meta: { ...out[card].meta, cardTurnOver: true } }
      card = -1
    }
  }
  return out
}

/** Draws a card row. It is the turn's live card while the slot runs and no
 *  later turn has started. */
export function crewmateBrowserLiveRenderer(sessionKey: string): MessageRenderer {
  return {
    id: CREWMATE_BROWSER_ROLE,
    roles: [CREWMATE_BROWSER_ROLE],
    render: (m, ctx) => {
      const anchor = browserLiveAnchor(m)
      const running = ctx.running && !m.meta?.cardTurnOver
      return ctx.row(<BrowserLiveCard sessionKey={sessionKey} anchorKey={anchor.key} anchorTs={anchor.ts} running={running} />)
    },
  }
}
