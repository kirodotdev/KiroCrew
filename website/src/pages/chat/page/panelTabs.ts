import { useRef, useCallback, type MutableRefObject } from 'react'
import { useQuery } from '@tanstack/react-query'
import type { AppDispatch, AppStore } from '../../../store'
import type { ChatSlot } from '../../../types'
import { openActivityPanel, mcpAppKey } from '../../../store/chatSlice'
import { api } from '../../../api/client'
import type { useChatPageResourcesController } from '../useChatPageResourcesController'
import { mcpAppTabTitle } from '../../../lib/mcpAppSrcdoc'
import { i18nT } from '../../../i18n/t'

interface RunInTerminalConfigOptions {
  slots: ChatSlot[]
  activeSlot: string | null
}

/**
 * The live values the run-in-terminal listener reads at event time: this chat's
 * project, and the terminal config (reuse, still loading, failed to load).
 *
 * Called from ChatPage at the position its statements held, so React registers its effects in the same order.
 */
export function useRunInTerminalConfig({
  slots, activeSlot,
}: RunInTerminalConfigOptions) {
  // The project ref is read by the resources controller's drop/paste handlers at
  // event time, so it is declared before the controller and refreshed every render.
  const currentProjectRef = useRef<string | undefined>(undefined)
  currentProjectRef.current = slots.find(s => s.key === activeSlot)?.project || undefined

  // Opt-in "reuse the current terminal" for Run-in-terminal
  // (dashboard.terminal.reuse_current, default off — the fresh-shell default is
  // unchanged). Read from the shared kirocrewConfig query and mirrored onto a
  // ref because the run-in-terminal handler (eventBridges.ts) installs once ([]-deps) and
  // reads it at event time, not at mount. Only a literal `true` turns it on —
  // the same "one literal decides it" rule the completion toggle uses, so a
  // hand-edited non-boolean cannot half-enable it.
  const { data: terminalCfg, isError: terminalCfgIsError, isPending: terminalCfgIsPending } = useQuery<{ dashboard?: { terminal?: { reuse_current?: boolean } } }>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
    staleTime: 30_000,
  })
  const terminalReuseRef = useRef(false)
  terminalReuseRef.current = terminalCfg?.dashboard?.terminal?.reuse_current === true
  // The handler installs once ([]-deps) and reads at event time, so the query's
  // UNSETTLED state is mirrored onto a ref too. Until the initial read settles
  // we cannot know whether reuse was saved on, and collapsing an unknown to the
  // fresh-tab EXECUTE path would run a command a reuse-on user expected only to
  // be copied — the exact harm this setting removes. So an unsettled read takes
  // the copy-never-run path (benign if reuse was actually off: the user just
  // pastes it themselves) rather than executing on a guess. `isPending` is true
  // only before the first result (data or error) arrives.
  const terminalCfgUnsettledRef = useRef(false)
  terminalCfgUnsettledRef.current = terminalCfgIsPending === true
  // The run-in-terminal handler installs once ([]-deps) and reads at event
  // time, so the query's FAILED state is mirrored onto a ref too. A read
  // failure is distinct from a legitimately-off setting: when the config could
  // not be read we cannot know whether reuse was on, so the handler surfaces
  // that read failure through ErrorNotice (errors-use-error-notice) rather than
  // silently collapsing a saved reuse-on into the fresh-tab path with nothing
  // on screen. `isError` only after the query has actually errored, so a
  // pending first load still takes the silent-default branch, not a false alarm.
  const terminalCfgReadFailedRef = useRef(false)
  terminalCfgReadFailedRef.current = terminalCfgIsError === true
  return {
    currentProjectRef, terminalReuseRef, terminalCfgUnsettledRef, terminalCfgReadFailedRef,
  }
}

interface AppPanelRevealOptions {
  dispatch: AppDispatch
  activeSlot: string | null
  boundStore: AppStore
  search: ReturnType<typeof useChatPageResourcesController>['search']
  tabsCtlRef: MutableRefObject<ReturnType<typeof useChatPageResourcesController>['tabsCtl']>
}

/**
 * Brings an MCP App's panel tab back from a bubble placeholder: re-opens the tab if
 * the user closed it and clears the find pane that would hide it.
 *
 * Called from ChatPage at the position its statements held, so React registers its effects in the same order.
 */
export function useAppPanelReveal({
  dispatch, activeSlot, boundStore, search, tabsCtlRef,
}: AppPanelRevealOptions) {
  /** Bring an app's panel tab back — focusing it if open, re-creating it if the
   *  user closed it (`openApp` upserts).
   *
   *  ChatPage's app auto-open effect deliberately does not re-open a tab the user
   *  closed, which is why the bubble placeholder has to be a real control rather
   *  than static text. Note the effect's once-per-tool-call guard holds only
   *  PER CHATPAGE MOUNT: `openedAppTabsRef` is not persisted, so navigating away
   *  and back re-arms it. Closing the find pane is part of the action: `isSidePanelHidden`
   *  keeps the panel hidden while search owns the dock, so without this the click
   *  would open a tab the user cannot see and look broken.
   *
   *  `close()` runs unconditionally, exactly as handleFileOpen / handleArtifactOpen /
   *  handleOpenDiff do. Guarding it on `search.isOpen` would pull that value into the
   *  closure, and `renderMessage` (transcriptRendering.tsx) holds this callback across renders where the
   *  find pane opens — so a captured `isOpen === false` would skip the close entirely
   *  and open the tab behind the hidden dock. `close()` is already safe with nothing
   *  open: it only hands focus back `if (wasOpen)`. */
  const revealAppInPanel = useCallback((toolCallId: string) => {
    search.close()
    dispatch(openActivityPanel())
    // Same title derivation as the auto-open effect: an event-time read from
    // the Provider-bound store keeps the payload out of this callback's deps.
    const payload = activeSlot ? boundStore.getState().chat.mcpApps?.[mcpAppKey(activeSlot, toolCallId)] : undefined
    tabsCtlRef.current?.openApp(toolCallId, mcpAppTabTitle(payload, i18nT('pages.chatPage.mcp_app_tab_title')), activeSlot ?? null)
    // As at handleFileOpen: the rule asks for the whole `search` object only because
    // `close` is INVOKED and a called member is attributed to its receiver, not
    // because this body reads `search` itself.
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `search.close` is a useCallback([]) in useMessageSearch, so the listed member already pins everything this body calls; naming the enclosing object would make this a new function every render and churn renderMessage below
  }, [dispatch, activeSlot, boundStore, search.close])
  return {
    revealAppInPanel,
  }
}
