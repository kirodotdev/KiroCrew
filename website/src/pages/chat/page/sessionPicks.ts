import { useQuery, type QueryClient } from '@tanstack/react-query'
import { useMemo, useRef, useState, useCallback, type Dispatch, type SetStateAction, type MutableRefObject } from 'react'
import type { ModelInfo } from '../../../providers/types'
import { store, type AppDispatch } from '../../../store'
import type { ChatSlot } from '../../../types'
import { useAnchoredTriggerRect } from '../../../hooks/useAnchoredTriggerRect'
import { resolveByApprovalId, setAgentSwitchNotice } from '../../../store/chatSlice'
import { removeNotificationByTs } from '../../../store/notificationsSlice'
import { updateSlot } from '../../../store/dashboardSlice'
import { inFlightSlotSwitchOutcome, stagedSlotSwitchTarget, performSlotSwitch } from '../../../lib/slotSwitch'
import { performAgentSlotSwitch } from '../../../lib/agentSwitch'
import { api } from '../../../api/client'
import { useFilteredDropdown } from '../../../hooks/useFilteredDropdown'
import { useModelPickerHiddenModelsQuery, useModelPickerConfigured, shouldSeparateModelEffort, filterInteractiveModels, effortToCarry, switchGroupedModel } from '../../../hooks/useInteractiveModels'
import { jevRouteOffered, withJevRoute, JEV_ROUTE_MODEL } from '../../../lib/jevRoute'
import { agentSwitchErrorNotice, agentSwitchFailureNotice, modelSwitchDeferredMessage } from '../../../utils/agentSwitchFeedback'
import { focusComposer } from '../composerFocus'
import { i18nT } from '../../../i18n/t'
import { fetchDashboardConfig } from '../../../api/dashboardConfigQuery'

interface ModelPickerModelsOptions {
  slots: ChatSlot[]
  activeSlot: string | null
  effectiveModels: ModelInfo[]
  /** The active slot's selection-capabilities read, from {@link useSlotSelectionCapabilitiesQuery}. */
  selectionCapabilitiesQ: ReturnType<typeof useSlotSelectionCapabilitiesQuery>
}

/**
 * The active slot's ACP selection capabilities. Read before the rosters, because
 * its `models_backend` decides which harness's model list the pickers offer.
 */
export function useSlotSelectionCapabilitiesQuery(activeSlot: string | null) {
  return useQuery({
    queryKey: ['slot-selection-capabilities', activeSlot],
    queryFn: () => api.chatSlotSelectionCapabilities(activeSlot!),
    enabled: !!activeSlot && typeof api.chatSlotSelectionCapabilities === 'function',
    // A new ACP session may not exist when its slot first appears. Recheck
    // until the agent reports its config options, then refresh less often.
    // A missing/non-ACP peer can remain unknown indefinitely. Probe quickly
    // during session startup, then back off instead of proxying every 2s.
    refetchInterval: query => query.state.data?.known || query.state.dataUpdateCount + query.state.errorUpdateCount >= 5 ? 30_000 : 2_000,
  })
}

/**
 * The model picker's roster: the slot's ACP selection capabilities, the hidden-model
 * list, the Auto (Jev) route offer, the filtered model list with its dropdown state, and
 * whether a pick hands focus back to the composer.
 *
 * Called from ChatPage at the position its statements held, so React registers its effects in the same order.
 */
export function useModelPickerModels({
  slots, activeSlot, effectiveModels, selectionCapabilitiesQ,
}: ModelPickerModelsOptions) {
  const selectionCapabilities = selectionCapabilitiesQ.data?.known
    ? selectionCapabilitiesQ.data
    : undefined
  const hiddenModelsQ = useModelPickerHiddenModelsQuery()
  const hiddenModelIds = hiddenModelsQ.data
  const modelPickerConfigured = useModelPickerConfigured()
  const availableModels = effectiveModels
  // The server owns the backend-specific model ID convention, including while
  // the ACP session is still starting. Never infer it from bracketed IDs alone.
  const codexPairModels = shouldSeparateModelEffort(
    selectionCapabilitiesQ.data?.model_effort_pair_ids, effectiveModels,
  )
  // Whether the picker may offer `Auto (Jev)` (see `lib/jevRoute.ts`): the fleet's
  // answer AND the owner's keystone consent, both required. Two reads the page
  // already makes for other reasons, so the row costs no new request.
  const jevDashCfgQ = useQuery<{ decisions_enabled?: boolean }>({
    queryKey: ['dashboardConfig'],
    queryFn: fetchDashboardConfig,
    staleTime: 30_000,
  })
  const jevConsentQ = useQuery({
    queryKey: ['decisionsConsent'],
    queryFn: () => api.getDecisionsConsent(),
    // A 404 is "this gateway predates the keystone", not a transient failure: the
    // row is simply not offered.
    retry: false,
  })
  // The slot answer is the third gate: without one the entry has nowhere to land.
  // It returns as soon as a slot exists.
  const jevRouteOn = jevRouteOffered(jevDashCfgQ.data, jevConsentQ.data, !!activeSlot)
  const jevRouteLabel = i18nT('pages.chatPage.model_auto_jev_description')
  const modelPickerModels = useMemo(
    () => {
      const pickerSlot = slots.find(slot => slot.key === activeSlot)
      return withJevRoute(
        filterInteractiveModels(effectiveModels, hiddenModelIds, [
          pickerSlot?.model || '',
          pickerSlot?.served_model || '',
        ], codexPairModels),
        jevRouteOn,
        jevRouteLabel,
      )
    },
    [effectiveModels, hiddenModelIds, slots, activeSlot, jevRouteOn, jevRouteLabel, codexPairModels],
  )
  const { open: modelDropdown, setOpen: setModelDropdown, filter: modelFilter, setFilter: setModelFilter, dropdownRef: modelDropdownRef, inputRef: modelInputRef, filtered: filteredModels } = useFilteredDropdown(modelPickerModels)
  // Whether the composer held focus when the picker was opened from its chip
  // (ChatInput reads this before the press moves focus). A pick closes the
  // picker, which unmounts the focused row and would otherwise drop focus on
  // <body>; when the user was typing, the pick hands focus back to the
  // composer so they can carry on. A user who was not typing is left alone —
  // focusing the composer under them would be a surprise, and on touch it
  // would raise the keyboard (`focusComposer` already skips touch).
  const modelPickerReturnsFocusRef = useRef(false)
  return {
    selectionCapabilitiesQ, selectionCapabilities, hiddenModelsQ, modelPickerConfigured, availableModels, codexPairModels, jevRouteOn, modelDropdown, setModelDropdown, modelFilter, setModelFilter,
    modelDropdownRef, modelInputRef, filteredModels, modelPickerReturnsFocusRef,
  }
}

interface NewSessionPicksOptions {
  modelDropdown: boolean
}

/**
 * The agent, model and project picked for the NEXT new session on a slotless page
 * (each a state value plus a ref the create path reads synchronously), and the model
 * picker's anchor on the composer chip.
 *
 * Called from ChatPage at the position its statements held, so React registers its effects in the same order.
 */
export function useNewSessionPicks({
  modelDropdown,
}: NewSessionPicksOptions) {
  const [pendingAgent, _setPendingAgent] = useState('')  // agent for next new slot
  const pendingAgentRef = useRef('')
  // The namespace the pending agent was picked from, kept beside the name so
  // the create carries both: a pending "reviewer" template must not become the
  // "reviewer" member on send. Cleared with the name.
  const pendingAgentKindRef = useRef<'member' | 'template' | undefined>(undefined)
  const setPendingAgent = useCallback((v: string, kind?: 'member' | 'template') => {
    pendingAgentRef.current = v
    pendingAgentKindRef.current = v ? kind : undefined
    _setPendingAgent(v)
  }, [])
  const [pendingModel, _setPendingModel] = useState('')  // model for next new slot
  const pendingModelRef = useRef('')
  const setPendingModel = useCallback((v: string) => { pendingModelRef.current = v; _setPendingModel(v) }, [])
  const pendingProjectRef = useRef('')
  const setPendingProject = useCallback((v: string) => { pendingProjectRef.current = v }, [])

  // pendingModel is the model for the NEXT new slot, and it is deliberately
  // left EMPTY unless the user explicitly picks one (switchModel below).
  //
  // It used to be seeded at mount from the backend resolver. That resolver
  // answers "what would run", which is right for the composer chip but wrong as
  // a session-create value: a session's model is a permanent pin (the runtime
  // reads `slot.model or agent_model`, so a set slot.model wins for every later
  // turn). Seeding it pinned every new chat to whatever the four-tier chain
  // happened to resolve at page load, so an agent left on Auto never
  // re-resolved and later changes to the agent or the global default never
  // reached the session (#2035).
  //
  // Sending nothing is what preserves the chain. `SessionManager.get_or_create`
  // documents that a `None` model "falls back to the global agent.model config
  // -- but only when the named agent does not pin its own model ... and the
  // global is not a sentinel value like 'auto', in which case it stays None to
  // let the backend resolve from the agent's own JSON config". So omitting it
  // honours the crew pin, the template pin, the global default and Auto, in that
  // order, at session-create time.
  //
  // Sending the literal 'auto' would NOT be equivalent: it is truthy, so it
  // short-circuits `slot.model or agent_model` and would override a template or
  // global pin the user did configure.
  // Composer-toolbar picker anchors: each hook keeps its portaled menu glued
  // to the ChatInput chip that opened it while the menu is open (#10616).
  const { rect: modelBtnRect, anchorTo: anchorModelBtn } = useAnchoredTriggerRect(modelDropdown)
  return {
    pendingAgent, pendingAgentRef, pendingAgentKindRef, setPendingAgent, pendingModel, pendingModelRef, setPendingModel, pendingProjectRef, setPendingProject, modelBtnRect, anchorModelBtn,
  }
}

interface SessionChoicesOptions {
  dispatch: AppDispatch
  queryClient: QueryClient
  activeSlot: string | null
  codexPairModels: boolean
  setModelDropdown: Dispatch<SetStateAction<boolean>>
  modelPickerReturnsFocusRef: MutableRefObject<boolean>
  setPendingAgent: (v: string, kind?: 'member' | 'template') => void
  setPendingModel: (v: string) => void
  setPendingProject: (v: string) => void
}

/**
 * The choices a user makes about this session: answering an approval, switching the
 * agent or the model (or, with no slot yet, staging them for the next one), and
 * picking the project.
 *
 * Called from ChatPage at the position its statements held, so React registers its effects in the same order.
 */
export function useSessionChoices({
  dispatch, queryClient, activeSlot, codexPairModels, setModelDropdown, modelPickerReturnsFocusRef, setPendingAgent, setPendingModel, setPendingProject,
}: SessionChoicesOptions) {
  const approve = useCallback(async (action: string) => { if (activeSlot) await api.approveChatSlot(activeSlot, action) }, [activeSlot])
  // Approvals dismissed through the CollapsibleToolGroup mounts resolve via the
  // ONE-SHOT `api.resolveApproval` endpoint, which has no trust verb. The shared
  // `toApiDecision` (utils/approvalDecision.ts) is fail-closed and is the only
  // place that mapping is spelled — a Trust affordance on this path would claim
  // a standing grant the backend never records (#5400, #5434).
  const dismissApproval = useCallback((aid: string, decision?: string) => {
    dispatch(resolveByApprovalId({ id: aid, slot: activeSlot || undefined, decision }))
    const n = store.getState().notifications.items.find(x => x.approval_id === aid)
    if (n) dispatch(removeNotificationByTs(n.ts))
  }, [activeSlot, dispatch])
  const switchAgent = useCallback(async (agentName: string, kind?: 'member' | 'template') => {
    if (!activeSlot) {
      setPendingAgent(agentName, kind)
      // Clear any explicit pick made for the PREVIOUS agent rather than
      // re-seeding a resolved model: an empty pendingModel makes createSlot omit
      // `model`, which lets the backend resolve the new agent's own chain at
      // create time. Seeding the resolved id here pinned it instead (#2035).
      setPendingModel('')
      return
    }
    dispatch(setAgentSwitchNotice(null))
    try {
      // Same protocol as switchModel below (#4523): the acting tab must not
      // depend on the coalesced slots rebroadcast to see its own pick.
      // performAgentSlotSwitch mirrors exactly what the response names.
      await performAgentSlotSwitch(activeSlot, agentName, dispatch, kind)
      // The cold effort answer was judged on the previous crew's model; without
      // this it holds until the next poll, up to 30 s once the poll backs off.
      void queryClient.invalidateQueries({ queryKey: ['slot-selection-capabilities', activeSlot] })
    } catch (error) {
      // Closing the picker is the call sites' job and already happens
      // synchronously alongside this call, so a failure surfaces as the shared
      // notice rather than by holding the dropdown open.
      dispatch(setAgentSwitchNotice(agentSwitchFailureNotice(error, agentName)))
    }
    // The setPending* setters are useState setters, so they are stable and cost
    // nothing to list, as is `queryClient`. `installedAgents` and `provider` are
    // deliberately absent: this body reads neither, and `installedAgents` is
    // a fresh array on every agents refetch, so naming it would rebuild the
    // callback — and every picker holding it — for no behavioral gain.
  }, [activeSlot, dispatch, queryClient, setPendingAgent, setPendingModel])
  const switchModel = useCallback(async (modelName: string) => {
    // 'auto' is stored VERBATIM, not collapsed to ''. Both resolve to the same
    // provider behaviour server-side, but '' is also the "never chosen" state,
    // and every reader of an empty model re-resolves it to the agent template's
    // model (the `resolvedModel` / `_initResolvedModel` queries in composerChips.ts, and the
    // backend's slot.model backfill). Writing '' therefore made an explicit Auto
    // pick snap straight back to e.g. claude-opus-5 — Auto was unselectable.
    // kiro-cli advertises `auto` as a real model id (and its default_model), and
    // the ChatPane + Alt+Shift model-cycle paths already send it verbatim.
    // `pendingModel` is forwarded into slot creation verbatim, and the sentinel is
    // not a model any provider serves. So it is held as NOTHING: creation omits the
    // model and the backend resolves the agent's own chain, which is what a held
    // `auto` would have resolved to anyway. Routing is armed by a pick made once the
    // slot exists, where the flag is set with it.
    if (!activeSlot) {
      if (modelName === JEV_ROUTE_MODEL) { setPendingModel(''); return }
      setPendingModel(modelName)
      return
    }
    try {
      const slotState = store.getState().dashboard.slots.find(s => s.key === activeSlot)
      // A staged slider pick or a legacy pair level is carried; an effort
      // write already on the wire is waited for, not re-sent, and its
      // presence blocks the legacy migration -- see effortToCarry.
      const inFlightEffort = inFlightSlotSwitchOutcome('reasoning_effort', activeSlot)
      const carriedEffort = effortToCarry(
        slotState?.model || '', slotState?.reasoning_effort || '',
        stagedSlotSwitchTarget('reasoning_effort', activeSlot),
        inFlightEffort !== null,
        codexPairModels,
      )
      await switchGroupedModel(carriedEffort, async level => {
        // Either the effort the user just picked in the embedded slider (still
        // inside its debounce, so not yet on the wire) or, on a Codex slot
        // that still pins model[max], that old pair level moving into the
        // separate effort field before a grouped row sends the bare model.
        // A failed effort write aborts the model pick instead of silently
        // resetting an owner's previous selection.
        let normalizedModel: string | undefined
        await performSlotSwitch('reasoning_effort', activeSlot, level,
          async () => {
            const r = await api.chatSlotReasoningEffort(activeSlot, level)
            normalizedModel = r?.model
            return r?.reasoning_effort ?? level
          },
          (value) => dispatch(updateSlot({
            key: activeSlot,
            reasoning_effort: value,
            ...(normalizedModel ? { model: normalizedModel } : {}),
          })))
      }, async afterEffort => {
        // performSlotSwitch owns the whole protocol: per-slot+field serialized
        // dispatch, latest-request-wins adjudication, hung-request timeout, and
        // exactly-one store write on the authoritative value (#4523). The store
        // write is deliberately NOT awaited on the server's slots rebroadcast:
        // that push is coalesced and never arrives with the websocket down.
        await performSlotSwitch('model', activeSlot, modelName,
          async () => {
            // The response's `model` is the stored value (deprecated ids are
            // remapped server-side), so prefer it over the requested name.
            // Effort first on the wire (see switchGroupedModel); a refused
            // effort aborts this pick before anything is sent.
            await afterEffort
            const r = await api.chatSlotModel(activeSlot, modelName)
            // A pick taken during a response is committed now and applies
            // from the next message; the store write below shows it at once,
            // so this line is what tells the user the running reply keeps its model.
            if (r?.deferred) dispatch(setAgentSwitchNotice(modelSwitchDeferredMessage(r.model ?? modelName)))
            return r?.model ?? modelName
          },
          // The routing flag is written from the REQUEST, not from the response's
          // `model`: the gateway resolves the sentinel to `auto`, so the stored
          // model cannot tell a routed pick from a plain Auto one. Written on
          // every pick, because picking a concrete model is what clears it.
          (value) => dispatch(updateSlot({
            key: activeSlot,
            model: value,
            jev_route: modelName === JEV_ROUTE_MODEL,
          })))
      }, () => inFlightSlotSwitchOutcome('reasoning_effort', activeSlot))
      queryClient.invalidateQueries({ queryKey: ['slot-selection-capabilities', activeSlot] })
    } catch (e) {
      // Same failure surface as the agent switch beside this: the shared
      // notice toast, preferring the server's own message. The chip keeps
      // showing what is actually running either way.
      dispatch(setAgentSwitchNotice(agentSwitchErrorNotice(e)))
      // eslint-disable-next-line no-console -- surface switchModel failures for debugging
      console.error('switchModel failed', e)
    }
    // Dismissal is the picker's job, not this callback's: a row click closes
    // the menu at the call site (the same shape as the agent picker and the
    // split-pane ChatPane picker), so a rejected switch is reported by the
    // notice toast above, never by a menu left open. Reasoning-effort edits
    // live on the drill-in page and keep the menu open on their own.
    // setPendingModel is a stable useState setter.
  }, [activeSlot, codexPairModels, dispatch, queryClient, setPendingModel])
  // A pick from the picker: a row click or Enter on the sole filtered match.
  // Closes the menu and, when the composer held focus at open time, hands
  // focus back to it (see `modelPickerReturnsFocusRef`). The picker's other
  // exits — Escape, outside click, the drill-in page's own links — are not
  // picks and keep their existing focus behaviour.
  const pickModel = useCallback((modelName: string) => {
    switchModel(modelName)
    setModelDropdown(false)
    if (modelPickerReturnsFocusRef.current) focusComposer()
  }, [switchModel, setModelDropdown, modelPickerReturnsFocusRef])
  const setProject = useCallback(async (path: string) => {
    if (!activeSlot) { setPendingProject(path); return }
    try {
      // Same protocol as switchModel above; the server realpath-normalizes
      // the directory, so the response's spelling is what gets written.
      await performSlotSwitch('project', activeSlot, path,
        async () => {
          const r = await api.chatSlotProject(activeSlot, path)
          return r?.project ?? path
        },
        (value) => dispatch(updateSlot({ key: activeSlot, project: value })))
    } catch (e) {
      dispatch(setAgentSwitchNotice(agentSwitchErrorNotice(e)))
      // eslint-disable-next-line no-console -- surface setProject failures for debugging
      console.error('setProject failed', e)
    }
    // setPendingProject is a stable ref-backed setter.
  }, [activeSlot, dispatch, setPendingProject])
  return {
    approve, dismissApproval, switchAgent, pickModel, setProject,
  }
}
