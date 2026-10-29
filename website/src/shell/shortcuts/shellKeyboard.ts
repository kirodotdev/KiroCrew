import { useCallback, useEffect, useState } from 'react'
import { agentCycleTarget } from '../../lib/crewMode'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useAppSelector, useAppDispatch, useAppStore, store } from '../../store'
import { changeApprovalMode, updateSlot } from '../../store/dashboardSlice'
import { requestSlotFolderOpen, setAgentSwitchNotice } from '../../store/chatSlice'
import { isStaleProjectDirError } from '../../utils/folderAgent'
import type { ChatFolder } from '../../types'
import { newChatShortcutArgs } from './newChatArgs'
import { createChatForView, showCreatedChat } from '../../lib/createdChatView'
import { useAskingNavigate } from '../../components/NavigationLeaveGuard'
import { pendingSlotSwitch, pendingSlotSwitchTarget, performSlotSwitch } from '../../lib/slotSwitch'
import { performAgentSlotSwitch } from '../../lib/agentSwitch'
import { focusComposerElement, queryComposerOrExpand } from '../../pages/chat/composerFocus'
import { agentSwitchErrorNotice, agentSwitchFailureNotice, modelSwitchDeferredMessage } from '../../utils/agentSwitchFeedback'
import { api } from '../../api/client'
import { newTerminalByChord, toggleTerminalByChord } from '../../lib/terminalChordFocus'
import { TOGGLE_SIDE_TERMINAL_EVENT } from '../../lib/panelToggleShortcuts'
import { focusPopout as focusTerminalPopout } from '../../utils/terminalPopout'
import { useKeyboardShortcuts } from '../../hooks/useKeyboardShortcuts'
import { useInstanceShortcuts } from '../../hooks/useInstanceShortcuts'
import { useAutoConnectInstances } from '../../hooks/useAutoConnectInstances'
import { useCommandPalette } from '../../hooks/useCommandPalette'
import { useProvider } from '../../providers/context'
import { useAgents } from '../../hooks/useAgents'
import { i18nT } from '../../i18n/t'
import { SESSION_FOLDER_UNAVAILABLE_EVENT } from '../../lib/sessionFolderNotice'

const REASONING_EFFORT_LEVELS = ['', 'low', 'medium', 'high', 'xhigh', 'max']
// Approval-mode DISCRIMINANTS in escalating order, cycled by keyboard shortcut.
// Sent to the backend and compared, never rendered — the picker has its own copy.
const APPROVAL_MODE_LEVELS = ['normal', 'trust_reads', 'trust', 'yolo']

/**
 * The shell's global keyboard entry points: the shortcuts modal, the command
 * palette, the new-chat chord, the agent / model / effort / approval-mode cycles
 * on the active session (and the notice a failed switch leaves), the panel
 * toggles, and the instance-pane and auto-connect hooks registered once here.
 */
export function useShellKeyboard({ toggleFocusMode, toggleNav, terminalEnabled, isPopout, isEmbed, terminalPoppedOut, activeSlotProject }: {
  toggleFocusMode: () => void
  toggleNav: () => void
  terminalEnabled: boolean
  isPopout: boolean
  isEmbed: boolean
  terminalPoppedOut: boolean
  activeSlotProject: string | undefined
}) {
  // The new-chat create can take seconds, so its navigate asks when it runs.
  const navigate = useAskingNavigate()
  const dispatch = useAppDispatch()
  // The Provider's store, so the create is judged against the store it was
  // dispatched to; the module singleton would bypass a test-injected store.
  const appStore = useAppStore()
  const queryClient = useQueryClient()
  const [shortcutsOpen, setShortcutsOpen] = useState(false)
  const toggleShortcutsModal = useCallback(() => setShortcutsOpen(p => !p), [])
  // Search Everywhere command palette — global double-Shift / ⌘K
  // trigger + open state. Mounted once at the app shell (`shell/appShell.tsx`).
  const commandPalette = useCommandPalette()
  const newChatMutation = useMutation({
    // The active chat's folder wins over the default folder (#16041).
    mutationFn: (args: ReturnType<typeof newChatShortcutArgs>) => createChatForView(dispatch, appStore.getState, args),
    onError: (err, args) => {
      // The one failure a folder create adds: the folder's project directory
      // is gone. The folder "+" says so under the folder header; the shortcut
      // has no header, so it uses the shell's transient notice.
      if ('project' in args && args.project && isStaleProjectDirError(err)) {
        dispatch(setAgentSwitchNotice(i18nT('pages.chatSidebar.folder_project_dir_missing', { path: args.project })))
      }
    },
    onSuccess: (view) => {
      // A create the user moved away from (another session, another page)
      // leaves the user there: no navigate and no composer focus. So does a
      // page that refused to be left.
      if (!showCreatedChat(view, navigate)) return
      // A chat filed into the active chat's folder: open that folder's path in
      // the sidebar so its row is not hidden (a default-folder create already
      // asks from createSlot; this is a no-op for an unfiled chat).
      if (view.slot.folder_id) dispatch(requestSlotFolderOpen({ key: view.slot.key, folder_id: view.slot.folder_id }))
      // Unguarded on purpose: this mutation only fires from the new-chat
      // keyboard shortcut, and a pressed shortcut proves a keyboard exists —
      // focusComposer()'s touch-device skip would wrongly suppress focus on a
      // tablet with a physical keyboard. Next frame, so the new slot's
      // composer has been committed to the DOM.
      //
      // Through the resolver rather than a bare lookup, so a composer the user
      // left collapsed is asked back instead of swallowing the caret: creating a
      // session IS a typing intent, and the alternative is a new chat whose
      // first keystroke goes nowhere.
      requestAnimationFrame(() => queryComposerOrExpand(focusComposerElement))
    },
  })
  const refreshTrigger = useAppSelector(s => s.dashboard.refreshTrigger)
  const { agents: installedAgents, defaultAgent } = useAgents(refreshTrigger)
  const provider = useProvider()
  const agentSwitchNotice = useAppSelector(s => s.chat.agentSwitchNotice)
  useEffect(() => {
    // A notice that offers a new chat stays until the user dismisses it or uses
    // the offer: expiring it would unmount the banner while its create runs and
    // hide a failed create.
    if (!agentSwitchNotice || agentSwitchNotice.newChatAgent) return
    const timer = window.setTimeout(() => dispatch(setAgentSwitchNotice(null)), 6000)
    return () => window.clearTimeout(timer)
  }, [agentSwitchNotice, dispatch])
  // A new chat that could not get its own session folder opened on the default
  // project; the API client announces it for every create path (see
  // lib/sessionFolderNotice), and it shares this transient notice.
  useEffect(() => {
    const onFolderUnavailable = () => dispatch(setAgentSwitchNotice(i18nT('store.chatSlice.session_folder_unavailable')))
    window.addEventListener(SESSION_FOLDER_UNAVAILABLE_EVENT, onFolderUnavailable)
    return () => window.removeEventListener(SESSION_FOLDER_UNAVAILABLE_EVENT, onFolderUnavailable)
  }, [dispatch])
  const switchActiveSlotAgent = useCallback(async (slot: string, agent: string) => {
    dispatch(setAgentSwitchNotice(null))
    try {
      // Same protocol as onCycleModel below (#4523): without the store write
      // the acting tab depends on the coalesced slots rebroadcast to see its
      // own pick. performAgentSlotSwitch mirrors exactly what the response
      // names ({agent, workspace} as one adjudicated pair; project is left
      // to the rebroadcast).
      await performAgentSlotSwitch(slot, agent, store.dispatch)
    } catch (error) {
      dispatch(setAgentSwitchNotice(agentSwitchFailureNotice(error, agent)))
    }
  }, [dispatch])
  useKeyboardShortcuts({ onToggleShortcutsModal: toggleShortcutsModal, onNewChat: () => {
    const s = appStore.getState()
    newChatMutation.mutate(newChatShortcutArgs(s.dashboard.slots, s.chat.activeSlot, queryClient.getQueryData<ChatFolder[]>(['chat-folders'])))
  }, disabled: shortcutsOpen,
    onToggleFocusMode: toggleFocusMode,
    onCycleAgent: () => {
      const slots = store.getState().dashboard.slots
      const activeSlot = store.getState().chat.activeSlot
      if (!activeSlot || installedAgents.length === 0) return
      const currentSlot = slots.find((s: { key: string }) => s.key === activeSlot)
      // Step from the newest in-flight target when one exists — see
      // onCycleModel below. Agent names are never '', so the ''-falsy
      // accessor is safe here.
      const currentAgent = pendingSlotSwitch('agent', activeSlot) || currentSlot?.agent || defaultAgent
      // The picker's own conductor-free list (lib/crewMode): a chord never moves a
      // normal chat onto a conductor, and a conductor chat does not cycle at all.
      const target = agentCycleTarget(installedAgents, currentAgent, currentSlot?.agent_kind, 1, currentSlot?.crew_mode_agent)
      if (target) void switchActiveSlotAgent(activeSlot, target)
    },
    onCyclePrevAgent: () => {
      const slots = store.getState().dashboard.slots
      const activeSlot = store.getState().chat.activeSlot
      if (!activeSlot || installedAgents.length === 0) return
      const currentSlot = slots.find((s: { key: string }) => s.key === activeSlot)
      // See onCycleAgent above.
      const currentAgent = pendingSlotSwitch('agent', activeSlot) || currentSlot?.agent || defaultAgent
      const target = agentCycleTarget(installedAgents, currentAgent, currentSlot?.agent_kind, -1, currentSlot?.crew_mode_agent)
      if (target) void switchActiveSlotAgent(activeSlot, target)
    },
    onCycleReasoningEffort: async () => {
      const activeSlot = store.getState().chat.activeSlot
      if (!activeSlot) return
      const slots = store.getState().dashboard.slots
      const currentSlot = slots.find((s: { key: string }) => s.key === activeSlot)
      // Step from the newest in-flight target (see onCycleModel below). ''
      // is a REAL effort target (provider default), so this base uses the
      // null-aware accessor — the ''-falsy one would misread an in-flight
      // "back to default" as "nothing pending" and mis-step the burst.
      const base = pendingSlotSwitchTarget('reasoning_effort', activeSlot)
        ?? (currentSlot?.reasoning_effort || '')
      const idx = REASONING_EFFORT_LEVELS.indexOf(base)
      const nextIdx = (idx + 1) % REASONING_EFFORT_LEVELS.length
      const level = REASONING_EFFORT_LEVELS[nextIdx]
      try {
        await performSlotSwitch('reasoning_effort', activeSlot, level,
          async () => {
            const r = await api.chatSlotReasoningEffort(activeSlot, level)
            return r?.reasoning_effort ?? level
          },
          (value) => store.dispatch(updateSlot({ key: activeSlot, reasoning_effort: value })))
      } catch (e) {
        store.dispatch(setAgentSwitchNotice(agentSwitchErrorNotice(e)))
        // eslint-disable-next-line no-console -- failure diagnostic; the notice above already told the user
        console.error('onCycleReasoningEffort failed', e)
      }
    },
    onCyclePrevReasoningEffort: async () => {
      const activeSlot = store.getState().chat.activeSlot
      if (!activeSlot) return
      const slots = store.getState().dashboard.slots
      const currentSlot = slots.find((s: { key: string }) => s.key === activeSlot)
      // See onCycleReasoningEffort above.
      const base = pendingSlotSwitchTarget('reasoning_effort', activeSlot)
        ?? (currentSlot?.reasoning_effort || '')
      const idx = REASONING_EFFORT_LEVELS.indexOf(base)
      const prevIdx = (idx - 1 + REASONING_EFFORT_LEVELS.length) % REASONING_EFFORT_LEVELS.length
      const level = REASONING_EFFORT_LEVELS[prevIdx]
      try {
        await performSlotSwitch('reasoning_effort', activeSlot, level,
          async () => {
            const r = await api.chatSlotReasoningEffort(activeSlot, level)
            return r?.reasoning_effort ?? level
          },
          (value) => store.dispatch(updateSlot({ key: activeSlot, reasoning_effort: value })))
      } catch (e) {
        store.dispatch(setAgentSwitchNotice(agentSwitchErrorNotice(e)))
        // eslint-disable-next-line no-console -- failure diagnostic; the notice above already told the user
        console.error('onCyclePrevReasoningEffort failed', e)
      }
    },
    onCycleApprovalMode: () => {
      const state = store.getState()
      const activeSlot = state.chat.activeSlot
      if (!activeSlot) return
      const current = state.dashboard.approvalMode || 'normal'
      const idx = APPROVAL_MODE_LEVELS.indexOf(current)
      const next = APPROVAL_MODE_LEVELS[(idx + 1) % APPROVAL_MODE_LEVELS.length]
      store.dispatch(changeApprovalMode({ mode: next, slot: activeSlot }))
    },
    onCyclePrevApprovalMode: () => {
      const state = store.getState()
      const activeSlot = state.chat.activeSlot
      if (!activeSlot) return
      const current = state.dashboard.approvalMode || 'normal'
      const idx = APPROVAL_MODE_LEVELS.indexOf(current)
      const prev = APPROVAL_MODE_LEVELS[(idx - 1 + APPROVAL_MODE_LEVELS.length) % APPROVAL_MODE_LEVELS.length]
      store.dispatch(changeApprovalMode({ mode: prev, slot: activeSlot }))
    },
    onCycleModel: async () => {
      const activeSlot = store.getState().chat.activeSlot
      if (!activeSlot) return
      const models = queryClient.getQueryData<{ name: string }[]>(['available-models', provider.id])
      if (!models || models.length === 0) return
      const slots = store.getState().dashboard.slots
      const currentSlot = slots.find((s: { key: string }) => s.key === activeSlot)
      // Step from the newest IN-FLIGHT target when one exists: each press of a
      // burst must advance one step even though the store base has not
      // settled yet — recomputing from the store made a rapid triple-press
      // send the same "next" three times and land one step ahead (#4523).
      const base = pendingSlotSwitch('model', activeSlot) || currentSlot?.model || ''
      const idx = base ? models.findIndex(m => m.name === base) : -1
      const nextIdx = (idx + 1) % models.length
      const name = models[nextIdx].name
      // Same protocol as ChatPage.switchModel (#4523): without the store
      // write a dead websocket wedges the cycle on one step; the shared
      // per-slot registry means neither the other cycle direction, the
      // dropdown, nor another slot's press can interleave stale.
      try {
        await performSlotSwitch('model', activeSlot, name,
          async () => {
            const r = await api.chatSlotModel(activeSlot, name)
            // Taken during a response: committed now, applies from the next message.
            if (r?.deferred) store.dispatch(setAgentSwitchNotice(modelSwitchDeferredMessage(r.model ?? name)))
            return r?.model ?? name
          },
          (value) => store.dispatch(updateSlot({ key: activeSlot, model: value })))
      } catch (e) {
        store.dispatch(setAgentSwitchNotice(agentSwitchErrorNotice(e)))
        // eslint-disable-next-line no-console -- failure diagnostic; the notice above already told the user
        console.error('onCycleModel failed', e)
      }
    },
    onCyclePrevModel: async () => {
      const activeSlot = store.getState().chat.activeSlot
      if (!activeSlot) return
      const models = queryClient.getQueryData<{ name: string }[]>(['available-models', provider.id])
      if (!models || models.length === 0) return
      const slots = store.getState().dashboard.slots
      const currentSlot = slots.find((s: { key: string }) => s.key === activeSlot)
      // See onCycleModel above.
      const base = pendingSlotSwitch('model', activeSlot) || currentSlot?.model || ''
      const idx = base ? models.findIndex(m => m.name === base) : -1
      const prevIdx = idx <= 0 ? models.length - 1 : idx - 1
      const name = models[prevIdx].name
      try {
        await performSlotSwitch('model', activeSlot, name,
          async () => {
            const r = await api.chatSlotModel(activeSlot, name)
            // Taken during a response: committed now, applies from the next message.
            if (r?.deferred) store.dispatch(setAgentSwitchNotice(modelSwitchDeferredMessage(r.model ?? name)))
            return r?.model ?? name
          },
          (value) => store.dispatch(updateSlot({ key: activeSlot, model: value })))
      } catch (e) {
        store.dispatch(setAgentSwitchNotice(agentSwitchErrorNotice(e)))
        // eslint-disable-next-line no-console -- failure diagnostic; the notice above already told the user
        console.error('onCyclePrevModel failed', e)
      }
    },
    // Panel toggles. The sidebar lives here in App; the session list and the
    // activity panel live on the chat page and already listen for these window
    // events (their in-header buttons dispatch the same ones).
    onToggleLeftSidebar: () => toggleNav(),
    onToggleSessionPanel: () => window.dispatchEvent(new Event('toggle-pin-chat-sidebar')),
    onToggleSidePanel: () => window.dispatchEvent(new Event('toggle-activity-panel')),
    // Same command as the nav rail's Terminal row: focus the popped-out window
    // when the panel lives there, otherwise toggle the docked panel with the
    // active session's project as the shell's cwd. Left undefined when the
    // terminal is disabled, which the hook reads as UNBOUND: the chord is not
    // claimed at all, so it falls through to the browser rather than being
    // swallowed on behalf of a panel the rest of the UI hides.
    //
    // Also unbound in a popout or embedded pane, which render no docked terminal
    // of their own. `useBottomTerminal`'s tab list is localStorage-backed AND
    // cross-window synced, while its `open` flag is per window and no surface
    // in a popout reads it: a chord there would flip a flag nothing renders,
    // and with an empty tab list would mint a shell into the shared list.
    onToggleTerminal: terminalEnabled && !isPopout && !isEmbed
      ? () => { if (terminalPoppedOut) focusTerminalPopout(); else toggleTerminalByChord(activeSlotProject) }
      : undefined,
    // VS Code's Create New Terminal: open the docked panel if needed and add a
    // tab in the active session's project. Same gating as the toggle above, and
    // a popped-out panel is focused rather than grown from out of sight.
    onNewTerminal: terminalEnabled && !isPopout && !isEmbed
      ? () => { if (terminalPoppedOut) focusTerminalPopout(); else newTerminalByChord(activeSlotProject) }
      : undefined,
    // The side panel's terminal for this session: the chat page answers. Same
    // gating as the dock toggle: a popout or embed hosts no side panel.
    onToggleSideTerminal: terminalEnabled && !isPopout && !isEmbed
      ? () => window.dispatchEvent(new Event(TOGGLE_SIDE_TERMINAL_EVENT))
      : undefined,
  })
  // Cmd+1..9 (⌘ mac / Ctrl win-linux) switches instance panes: 1=Local,
  // 2=first remote, … — matching the InstanceTabBar left-to-right tab order.
  // Registered here (once) rather than in InstanceTabBar, which can mount more
  // than once (strip + inline header copies).
  useInstanceShortcuts()

  // Proactively bring remote-crew tunnels up on web-app load and on tab focus
  // (behind the default-on mc-auto-connect setting), so a crew is live without
  // a manual switcher click. Registered here once, like useInstanceShortcuts.
  useAutoConnectInstances()
  return { shortcutsOpen, setShortcutsOpen, toggleShortcutsModal, commandPalette, agentSwitchNotice }
}
