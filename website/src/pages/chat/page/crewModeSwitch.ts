/**
 * Crew Mode on the composer (CrewModeChip): whether the switch is offered for
 * this chat, whether it is on, the toggle, and the side panel task board it
 * opens.
 *
 * A UI over the existing conductor. A chat is in Crew Mode exactly when it runs
 * a conductor template (lib/crewMode: `runsConductor`), so flipping it is the ordinary agent switch
 * and there is no other Crew Mode state. The composer exists only for an open
 * session, so this reads the session itself, never a pending pick.
 *
 * The task board is the side panel's existing Dashboard view (`command-center`),
 * whose Overview is the session's own Dynamic Dashboard. It is opened the way
 * every other caller outside the panel opens a view (open the panel, then
 * `openView`), and only for a ROOT session (`isRootSession`): any other session
 * has no Overview of its own, and the gateway would refuse the read.
 */
import { useCallback, useState } from 'react'
import type { ChatSlot } from '../../../types'
import type { ViewKind } from '../../../hooks/usePanelTabs'
import { useAppDispatch } from '../../../store'
import { openActivityPanel } from '../../../store/chatSlice'
import { crewModeAgent, isDefaultAgentName, memoryModeAllowsCrewMode, runsConductor } from '../../../lib/crewMode'
import { isRootSession } from '../command-center/model'

export function useCrewModeSwitch({ currentSlot, defaultAgent, switchAgent, openView }: {
  currentSlot: ChatSlot | undefined
  /** The catalog's default template: the agent a plain chat runs on. */
  defaultAgent: string
  switchAgent: (agentName: string, kind?: 'member' | 'template') => Promise<void>
  /** The side panel's view opener (`usePanelTabs().openView`). */
  openView: (kind: ViewKind) => void
}) {
  const dispatch = useAppDispatch()
  // Offered on a plain local, non-app, Persistent chat that runs on the default
  // template (a plain session is never a crewmate) or already on the conductor.
  // Incognito and Temporary chats are left out: such a session may not open
  // sessions, so a conductor there could not conduct. With the conductor itself
  // configured as the default agent there is nothing to switch back to, so the
  // switch is not offered at all.
  const crewModeEligible = !!currentSlot && !runsConductor(defaultAgent, 'template') && !currentSlot.mode && !currentSlot.instance_id && currentSlot.origin !== 'app'
    && memoryModeAllowsCrewMode(currentSlot.memory_mode)
    // Crew Mode is a conductor TEMPLATE: `runsConductor` reads a crewmate that
    // carries a conductor's name as that crewmate, not Crew Mode.
    && (runsConductor(currentSlot.agent, currentSlot.agent_kind)
      || (currentSlot.agent_kind !== 'member' && isDefaultAgentName(currentSlot.agent ?? '', defaultAgent)))
  const crewModeOn = crewModeEligible && runsConductor(currentSlot?.agent, currentSlot?.agent_kind)
  const taskBoardOffered = crewModeEligible && isRootSession(currentSlot)
  const openTaskBoard = useCallback(() => {
    dispatch(openActivityPanel())
    openView('command-center')
  }, [dispatch, openView])
  // The chat whose switch to the conductor is in flight. The composer's chips go
  // the moment the chip is pressed, not when the switch lands: `switchAgent`
  // writes the slot from the server's answer before it settles, so on success the
  // slot is already on the conductor when this clears, and on a refused switch
  // (which `switchAgent` reports as its own notice) the chat never turned on and
  // its chips come back.
  const [turningOn, setTurningOn] = useState<string | null>(null)
  // One way only: the composer offers no way back to a plain chat. The template
  // namespace is named, since a bare name would be read as a crewmate whenever the
  // slot's kind is unset. Turning it on also opens the task board.
  const turnOnCrewMode = useCallback(() => {
    if (crewModeOn || !currentSlot) return
    const key = currentSlot.key
    setTurningOn(key)
    void switchAgent(crewModeAgent(), 'template').finally(() => setTurningOn(k => (k === key ? null : k)))
    if (taskBoardOffered) {
      openTaskBoard()
      // The Workers tab lands beside the board and in front: it is where the
      // chats this one opens are listed (they are not rows of the session list).
      openView('workers')
    }
  }, [crewModeOn, currentSlot, switchAgent, taskBoardOffered, openTaskBoard, openView])
  /** Crew Mode is on, or being turned on, for this chat: the composer then shows
   *  neither the memory-mode chip nor the Crew Mode chip, and never again. */
  const crewModeChosen = crewModeOn || (!!currentSlot && turningOn === currentSlot.key)
  return { crewModeEligible, crewModeOn, crewModeChosen, turnOnCrewMode, openTaskBoard: taskBoardOffered ? openTaskBoard : undefined }
}
