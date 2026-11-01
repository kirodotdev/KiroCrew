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
 * The task board is the side panel's Board tab (`workers`, the Ultra Mode crew
 * view). It is opened the way
 * every other caller outside the panel opens a view (open the panel, then
 * `openView`), and only for a ROOT session (`isRootSession`): any other session
 * has no Overview of its own, and the gateway would refuse the read.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import type { ChatSlot } from '../../../types'
import { dropPanelViews, panelStripOf, type ViewKind } from '../../../hooks/usePanelTabs'
import { useAppDispatch, useAppSelector } from '../../../store'
import { closeActivityPanel, openActivityPanel } from '../../../store/chatSlice'
import { crewModeAgent, gatewayRunsCrewMode, isDefaultAgentName, memoryModeAllowsCrewMode, runsConductor, runsCrewMode } from '../../../lib/crewMode'
import { isRootSession } from '../command-center/model'
import { useIsMobile } from '../../../hooks/useIsMobile'

const EMPTY: ReadonlySet<string> = new Set()
// The tabs Ultra Mode puts on a chat's strip: Crew and Dashboard.
const BOARD_VIEWS: readonly ViewKind[] = ['workers', 'command-center']
const withKey = (set: ReadonlySet<string>, key: string): ReadonlySet<string> => (set.has(key) ? set : new Set(set).add(key))
const withoutKey = (set: ReadonlySet<string>, key: string): ReadonlySet<string> => {
  if (!set.has(key)) return set
  const next = new Set(set)
  next.delete(key)
  return next
}

export function useCrewModeSwitch({ currentSlot, defaultAgent, hasUserMessage, switchAgent, openView }: {
  currentSlot: ChatSlot | undefined
  /** The catalog's default template: the agent a plain chat runs on. */
  defaultAgent: string
  /** The chat's transcript has a user message: Crew Mode can no longer be turned off. */
  hasUserMessage: boolean
  /** Resolves `false` when the switch was refused (it shows its own notice). */
  switchAgent: (agentName: string, kind?: 'member' | 'template') => Promise<boolean>
  /** The side panel's view opener (`usePanelTabs().openView`). */
  openView: (kind: ViewKind) => void
}) {
  const dispatch = useAppDispatch()
  // Offered on a plain local, non-app, Persistent chat that runs on the default
  // template (a plain session is never a crewmate) or already on the conductor.
  // Incognito and Temporary chats are left out: such a session may not open
  // sessions, so a conductor there could not conduct. With the conductor itself
  // configured as the default agent there is nothing to switch back to, so the
  // switch is not offered at all. A gateway whose agent catalog carries no Crew
  // Mode fields (one that predates it) runs no conductor: nothing is offered
  // there, so a press can never switch a chat onto a template the gateway does
  // not treat as Crew Mode.
  const plainChat = !!currentSlot && gatewayRunsCrewMode() && !currentSlot.mode && !currentSlot.instance_id && currentSlot.origin !== 'app'
    && memoryModeAllowsCrewMode(currentSlot.memory_mode)
  // Every conductor TEMPLATE is Crew Mode: `runsCrewMode` reads a crewmate that
  // carries a conductor's name as that crewmate, not as a conductor. Whether the
  // chat IS in Crew Mode does not depend on the switch being offered: a chat
  // opened on a conductor that is itself the default agent still has the Crew
  // Mode welcome, its task board, and no memory-mode chip.
  const crewModeOn = plainChat && runsCrewMode(currentSlot?.agent, currentSlot?.agent_kind, currentSlot?.crew_mode_agent)
  const crewModeEligible = plainChat && !runsConductor(defaultAgent, 'template')
    && (crewModeOn || (currentSlot?.agent_kind !== 'member' && isDefaultAgentName(currentSlot?.agent ?? '', defaultAgent)))
  const taskBoardOffered = (crewModeEligible || crewModeOn) && isRootSession(currentSlot)
  // The welcome page's Board button: the panel on the Board tab (`workers`), in
  // front. On a phone too, as the panel sheet: the person pressed it.
  const openTaskBoard = useCallback(() => {
    dispatch(openActivityPanel())
    openView('workers')
  }, [dispatch, openView])
  // The chats whose switch to the conductor is in flight, by slot key: a press in
  // another chat while one is in flight must not drop the first chat's hold. The
  // composer's chips go the moment the chip is pressed, not when the switch
  // lands: `switchAgent` writes the slot from the server's answer before it
  // settles, so on success the slot is already on the conductor when its entry
  // clears, and on a refused switch (which `switchAgent` reports as its own
  // notice) the chat never turned on and its chips come back.
  const [turningOn, setTurningOn] = useState<ReadonlySet<string>>(EMPTY)
  const activityOpen = useAppSelector(s => s.chat.activityOpen)
  // The chats whose turn-on found the panel closed, by slot key: a switch that
  // lands for one chat must not clear another chat's flag.
  const panelWasClosed = useRef<Set<string>>(new Set())
  // The chat on screen now, read when a switch settles after the person may have moved on.
  const shownKey = useRef<string | undefined>(currentSlot?.key)
  // Cleared on unmount: a switch that lands after the page has gone shows no chat.
  useEffect(() => {
    shownKey.current = currentSlot?.key
    return () => { shownKey.current = undefined }
  }, [currentSlot?.key])
  // entering an Ultra Mode root (new or existing) opens the side
  // panel on the Crew tab, once per visit. Not on a phone: there the panel is
  // a sheet over the chat, and a phone always lands on the chat.
  const isMobile = useIsMobile()
  const enteredKey = useRef<string | null>(null)
  const rootKey = crewModeOn && isRootSession(currentSlot) ? currentSlot!.key : null
  useEffect(() => {
    if (!rootKey || enteredKey.current === rootKey) { if (!rootKey) enteredKey.current = null; return }
    enteredKey.current = rootKey
    if (isMobile) return
    dispatch(openActivityPanel())
    openView('workers')
  }, [rootKey, dispatch, openView, isMobile])
  // The chats whose switch back to the default agent is in flight: their chips
  // come back the moment the mark is pressed, the same way they leave on turning on.
  const [turningOff, setTurningOff] = useState<ReadonlySet<string>>(EMPTY)
  // The template namespace is named, since a bare name would be read as a crewmate
  // whenever the slot's kind is unset. Turning it on also opens the task board.
  const turnOnCrewMode = useCallback(() => {
    if (crewModeOn || !currentSlot) return
    const key = currentSlot.key
    setTurningOn(set => withKey(set, key))
    // What the strip held before, so a refused switch (shown as its own notice)
    // can put it back: it removes only the tabs this press added, closes the
    // panel only if this press opened it and this chat is still on screen, and
    // brings back the tab that was in front.
    const opensBoard = taskBoardOffered && !isMobile
    const before = opensBoard ? panelStripOf(key) : null
    const added = before ? BOARD_VIEWS.filter(kind => !before.ids.includes(kind)) : []
    void switchAgent(crewModeAgent(), 'template').then(ok => {
      if (ok || !before) return
      if (panelWasClosed.current.delete(key) && shownKey.current === key) dispatch(closeActivityPanel())
      // Crew is the tab the press put in front: the tab that was in front before
      // comes back unless the person picked another one meanwhile.
      dropPanelViews(key, added, { focus: before.activeId, over: ['workers'] })
    }).finally(() => setTurningOn(set => withoutKey(set, key)))
    // the side panel opens on its own with the Crew tab in front
    // (SidePanel pins it first). Remember whether it was closed, so turning
    // Ultra Mode off again before the first message puts it back. Not on a
    // phone: there the panel is a sheet over the chat, so the chat stays in
    // front and the Board is one press away on the panel button.
    if (opensBoard) {
      if (activityOpen) panelWasClosed.current.delete(key)
      else panelWasClosed.current.add(key)
      dispatch(openActivityPanel())
      // The Dashboard tab still opens as before; Crew (pinned first) is in front.
      openView('command-center')
      openView('workers')
    }
  }, [crewModeOn, currentSlot, switchAgent, taskBoardOffered, dispatch, openView, activityOpen, isMobile])
  const crewModeLeaving = !!currentSlot && turningOff.has(currentSlot.key)
  const crewModeArriving = !!currentSlot && turningOn.has(currentSlot.key)
  /** Crew Mode is on, or being turned on, for this chat: the composer then shows
   *  neither the memory-mode chip nor the Crew Mode chip. */
  const crewModeChosen = (crewModeOn || crewModeArriving) && !crewModeLeaving
  /** The switch either way is in flight for this chat. The composer is held
   *  meanwhile, the way it is during a memory-mode switch: a send that reached
   *  the server first would start the turn on the agent the chat is leaving, and
   *  the switch would then be refused (the server switches no chat mid-turn). The
   *  draft stays in the box; `null` when nothing is in flight. */
  const crewModeSwitching: 'on' | 'off' | null = crewModeLeaving ? 'off' : crewModeArriving ? 'on' : null
  // Until the first user message Crew Mode can be turned off again (the server
  // allows it only then): back to the default agent this chat had before, which
  // is the only agent the switch is offered on. After that it is locked.
  // Never when the default agent is itself a conductor: there is nothing to go back to.
  const crewModeCancellable = crewModeEligible && crewModeOn && !crewModeLeaving && !hasUserMessage && !currentSlot?.running
  const turnOffCrewMode = useCallback(() => {
    if (!crewModeCancellable || !currentSlot) return
    const key = currentSlot.key
    setTurningOff(set => withKey(set, key))
    // The Crew and Dashboard tabs go only once the switch has landed: a refused
    // switch (shown as its own notice) leaves the chat in Ultra Mode, and it
    // keeps its tabs. The panel closes only if this chat is still the one on
    // screen, since the panel is shared by every chat.
    void switchAgent(defaultAgent || 'kirocrew', 'template').then(ok => {
      if (!ok) return
      if (panelWasClosed.current.delete(key) && shownKey.current === key) dispatch(closeActivityPanel())
      dropPanelViews(key, BOARD_VIEWS)
    }).finally(() => setTurningOff(set => withoutKey(set, key)))
  }, [crewModeCancellable, currentSlot, switchAgent, defaultAgent, dispatch])
  return {
    crewModeEligible, crewModeOn, crewModeChosen, crewModeLeaving, crewModeSwitching, turnOnCrewMode,
    turnOffCrewMode: crewModeCancellable ? turnOffCrewMode : undefined,
    openTaskBoard: taskBoardOffered ? openTaskBoard : undefined,
  }
}
