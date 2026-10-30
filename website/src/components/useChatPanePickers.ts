import { useState, useCallback, useEffect, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { modelHealthKey, useModelsDegraded } from '../providers/modelListHealth'
import { defaultWriteFailureOf, type DefaultWriteFailure } from './AgentDropdownList'
import { agentSwitchErrorNotice, agentSwitchFailureNotice, modelSwitchDeferredMessage } from '../utils/agentSwitchFeedback'
import { useProvider } from '../providers'
import { useAgents } from '../hooks/useAgents'
import { useFilteredDropdown } from '../hooks/useFilteredDropdown'
import { useAnchoredTriggerRect } from '../hooks/useAnchoredTriggerRect'
import { useAvailableModelsQuery } from '../hooks/useAvailableModels'
import { effortToCarry, filterInteractiveModels, legacyCodexEffort, modelWithoutEffort, shouldSeparateModelEffort, switchGroupedModel, useModelPickerConfigured, useModelPickerHiddenModelsQuery } from '../hooks/useInteractiveModels'
import { modelSupportsEffort, selectionCapabilitiesFailed } from '../lib/effort'
import { JEV_ROUTE_MODEL, jevRouteOffered, withJevRoute } from '../lib/jevRoute'
import { useListboxKeyboard } from '../hooks/useListboxKeyboard'
import { useKirocrewConfigReader } from '../hooks/useKirocrewConfigReader'
import { useImeGuard } from '../hooks/useImeGuard'
import { useScrollEdgesY } from '../hooks/useScrollEdges'
import { useAppSelector, type AppDispatch, type RootState } from '../store'
import { setAgentSwitchNotice } from '../store/chatSlice'
import { triggerRefresh, updateSlot } from '../store/dashboardSlice'
import { inFlightSlotSwitchOutcome, performSlotSwitch, stagedSlotSwitchTarget } from '../lib/slotSwitch'
import { performAgentSlotSwitch } from '../lib/agentSwitch'
import { api } from '../api/client'
import { displayModel, modelChipMarker } from '../lib/model'
import { useSettingsDefaultModel } from '../hooks/useSettingsDefaultModel'
import { i18nT } from '../i18n/t'
import { slotRowByKey } from '../store/slotIndex'

/**
 * `ChatPane`'s agent and model pickers, bound to the pane's own slot through
 * the same hooks and data sources ChatPage uses. `useChatPanePickerModels` is
 * what the pickers and the composer chips show: the agent roster and default,
 * the model list (hidden models, the `Auto (Jev)` row, codex pair models), the
 * model that will actually run, effort support and level, and the chip
 * markers. `useChatPanePickerActions` is what a pick does: the agent and model
 * switch protocols, their in-pane failure notice, and the menus' roving-focus
 * keys. `ChatPanePickerMenus` draws the two menus from both results.
 */

type PaneSlot = RootState['dashboard']['slots'][number] | undefined

export function useChatPanePickerModels({ slotKey, paneSlot, provider, dispatch, dashCfg }: {
  slotKey: string
  paneSlot: PaneSlot
  provider: ReturnType<typeof useProvider>
  dispatch: AppDispatch
  dashCfg: { decisions_enabled?: boolean } | undefined
}) {
  // Pickers — same hooks/data sources ChatPage uses, but selection targets THIS slot.
  // Subscribes to the store's global refresh so a default-agent write in ANY pane (or
  // in single chat) lands here too; a per-hook refresh would leave sibling pickers stale.
  const agentsRefreshTrigger = useAppSelector((s) => s.dashboard.refreshTrigger ?? 0)
  // This pane takes no project prop, so read THIS slot's project from the store:
  // it scopes which project-local agents exist, so a project change must refetch.
  const paneProject = useAppSelector((s) => slotRowByKey(s.dashboard.slots, slotKey)?.project || undefined)
  const { agents: installedAgents, choices: catalogChoices, defaultAgent } = useAgents(agentsRefreshTrigger, slotKey, paneProject)
  // The picker lists every catalog row (a member and a template of one name
  // are two rows). A roster source that exposes only the folded list -- one
  // row per name -- is still a complete, if namespace-blind, catalog.
  const agentChoices = catalogChoices ?? installedAgents
  // One source for every same-meaning marker: the composer chip, the row's
  // check, and the default-agent row's label. An agent-less slot resolves to
  // the configured default (matching what dispatch runs) before the literal
  // 'default' placeholder.
  const paneAgentName = paneSlot?.agent || defaultAgent || 'default'
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [defaultAgentFailed, setDefaultAgentFailed] = useState<DefaultWriteFailure | false>(false)
  // Same contract as ChatPage: set-only, clearing lives on the Templates page.
  const toggleDefaultAgent = useCallback((name: string, kind?: 'member' | 'template') => {
    setDefaultAgentFailed(false)
    Promise.resolve(api.setDefaultAgent?.(name, kind))
      .then(() => dispatch(triggerRefresh()))
      .catch((err: unknown) => setDefaultAgentFailed(defaultWriteFailureOf(err, name)))
  }, [dispatch])
  // The pop-up lists the full catalog (a same-name member and template are
  // two rows); every other reader of the roster keeps the name-folded list.
  const agentDD = useFilteredDropdown(agentChoices)
  const selectionCapabilitiesQ = useQuery({
    queryKey: ['slot-selection-capabilities', slotKey],
    queryFn: () => api.chatSlotSelectionCapabilities(slotKey),
    enabled: !!paneSlot && typeof api.chatSlotSelectionCapabilities === 'function',
    refetchInterval: query => query.state.data?.known || query.state.dataUpdateCount + query.state.errorUpdateCount >= 5 ? 30_000 : 2_000,
  })
  const ownModelsQuery = useAvailableModelsQuery({ backend: selectionCapabilitiesQ.data?.models_backend })
  const ownModelsFailed = selectionCapabilitiesQ.data?.models_backend !== undefined
    && (ownModelsQuery.isError || ownModelsQuery.isDegraded)
  const effectiveModels = ownModelsQuery.data
  const selectionCapabilities = selectionCapabilitiesQ.data?.known ? selectionCapabilitiesQ.data : undefined
  const codexPairModels = shouldSeparateModelEffort(
    selectionCapabilitiesQ.data?.model_effort_pair_ids, effectiveModels,
  )
  const hiddenModelsQ = useModelPickerHiddenModelsQuery()
  const hiddenModelIds = hiddenModelsQ.data
  const modelPickerConfigured = useModelPickerConfigured()
  const availableModels = effectiveModels
  // Same two reads and the same fail-closed rule as ChatPage: a split pane is
  // another view of the same sessions, so it offers the same row or the picker
  // would disagree with itself about whether routing is available. The dashboard
  // config comes from ChatPane.tsx's EXISTING observer (widened there) rather
  // than a second one on the same key.
  const jevConsentQ = useQuery({
    queryKey: ['decisionsConsent'],
    queryFn: () => api.getDecisionsConsent(),
    retry: false,
  })
  const jevRouteOn = jevRouteOffered(dashCfg, jevConsentQ.data, !!paneSlot)
  const jevRouteLabel = i18nT('pages.chatPage.model_auto_jev_description')
  const modelPickerModels = useMemo(
    () => withJevRoute(
      filterInteractiveModels(effectiveModels, hiddenModelIds, [
        paneSlot?.model || '',
        paneSlot?.served_model || '',
      ], codexPairModels),
      jevRouteOn,
      jevRouteLabel,
    ),
    [effectiveModels, hiddenModelIds, paneSlot?.model, paneSlot?.served_model, jevRouteOn, jevRouteLabel, codexPairModels],
  )
  const modelDD = useFilteredDropdown(modelPickerModels)
  const [attachModelList, modelListEdges, remeasureModelList] = useScrollEdgesY<HTMLDivElement>()
  // The filter box narrows `modelDD.filtered` without a scroll or a box
  // resize once the list sits at its max height, so re-measure on every
  // render as well.
  useEffect(remeasureModelList)
  // Picker anchors: keep each portaled menu glued to the ChatInput chip that
  // opened it while the menu is open (#10616, same class as #10580).
  const { rect: agentBtnRect, anchorTo: anchorAgentBtn } = useAnchoredTriggerRect(agentDD.open)
  const { rect: modelBtnRect, anchorTo: anchorModelBtn } = useAnchoredTriggerRect(modelDD.open)
  // See ChatPage: display what will actually run, not a pin the account lost
  // access to. The slot's own `model_withheld` verdict answers that when the
  // backend has one; the degraded flag gates only the list-membership fallback —
  // a cached list served while /api/models fails is stale and cannot disprove
  // entitlement — and is subscribed to, since it can flip while the served list
  // stays identical.
  const _modelsDegraded = useModelsDegraded(modelHealthKey(provider.id, selectionCapabilitiesQ.data?.models_backend))
  const displayModels = codexPairModels
    ? filterInteractiveModels(availableModels, [], [], true)
    : availableModels
  const shownModel = displayModel(
    codexPairModels ? modelWithoutEffort(paneSlot?.model || '') : paneSlot?.model || '',
    displayModels,
    _modelsDegraded,
    paneSlot?.model_withheld,
    codexPairModels ? modelWithoutEffort(paneSlot?.served_model || '') : paneSlot?.served_model || '',
  )
  // An unknown effort verdict leaves the currently shown model to answer.
  const effortCaps = selectionCapabilities ?? selectionCapabilitiesQ.data
  const effortSupported = provider.capabilities.reasoningEffort && !selectionCapabilitiesFailed(selectionCapabilitiesQ) && (
    effortCaps?.effort_supported != null
      ? effortCaps.effort_supported
      : modelSupportsEffort(shownModel === 'auto' ? '' : shownModel)
  )
  const effortLevelsOverride = effortCaps?.effort_levels?.length ? effortCaps.effort_levels : undefined
  const readKirocrewConfig = useKirocrewConfigReader()
  const { data: defaultEffort = '' } = useQuery({
    queryKey: ['default-effort', provider.id],
    queryFn: () => provider.resolveDefaultEffort(readKirocrewConfig),
    enabled: provider.capabilities.reasoningEffort,
  })
  const effectiveEffort = paneSlot?.reasoning_effort || legacyCodexEffort(
    paneSlot?.model || '', '', codexPairModels,
  ) || defaultEffort
  // What the pin alone would say; differing from `shownModel` means the chip
  // is naming the served default an inheriting slot runs on (see ChatPage).
  const _pinShownModel = displayModel(
    codexPairModels ? modelWithoutEffort(paneSlot?.model || '') : paneSlot?.model || '',
    displayModels,
    _modelsDegraded,
    paneSlot?.model_withheld,
  )
  // `default` only for the Settings default (see ChatPage).
  const chipDefault = useSettingsDefaultModel(
    paneSlot && !paneSlot.model ? paneAgentName : '',
    codexPairModels,
  )
  const modelMarker = modelChipMarker(
    paneSlot?.model || '',
    shownModel,
    _pinShownModel,
    chipDefault.settingsDefault,
    chipDefault.agentPinned,
  )

  return {
    installedAgents, agentChoices, defaultAgent, paneAgentName, queryClient, navigate, defaultAgentFailed, toggleDefaultAgent, agentDD,
    selectionCapabilitiesQ, ownModelsQuery, ownModelsFailed, codexPairModels, hiddenModelsQ, modelPickerConfigured, jevRouteOn, modelDD,
    attachModelList, modelListEdges, agentBtnRect, anchorAgentBtn, modelBtnRect, anchorModelBtn, shownModel,
    effortSupported, effortLevelsOverride, defaultEffort, effectiveEffort, chipDefault, modelMarker,
  }
}

type PickerModels = ReturnType<typeof useChatPanePickerModels>

export function useChatPanePickerActions({ slotKey, paneSlot, dispatch, queryClient, codexPairModels, agentDD, modelDD }: {
  slotKey: string
  paneSlot: PaneSlot
  dispatch: AppDispatch
  queryClient: QueryClient
  codexPairModels: PickerModels['codexPairModels']
  agentDD: PickerModels['agentDD']
  modelDD: PickerModels['modelDD']
}) {
  // In-pane report of a per-slot setting write (agent / model switch) that did
  // not persist — the shared toast is transient feedback, not the error surface.
  const [switchError, setSwitchError] = useState('')
  const switchAgent = useCallback(async (name: string, kind?: 'member' | 'template') => {
    dispatch(setAgentSwitchNotice(null))
    setSwitchError('')
    try {
      // Same protocol as switchModel below (#4523): the pane must not depend
      // on the coalesced slots rebroadcast to see its own pick.
      // performAgentSlotSwitch mirrors exactly what the response names.
      await performAgentSlotSwitch(slotKey, name, dispatch, kind)
      // The cold effort answer was judged on the previous crew's model.
      void queryClient.invalidateQueries({ queryKey: ['slot-selection-capabilities', slotKey] })
    } catch (e) {
      const notice = agentSwitchFailureNotice(e, name)
      dispatch(setAgentSwitchNotice(notice))
      setSwitchError(notice.message)
    }
  }, [dispatch, queryClient, slotKey])
  const switchModel = useCallback(async (name: string) => {
    setSwitchError('')
    try {
      // A staged slider pick or a legacy pair level is carried; an effort
      // write already on the wire is waited for, not re-sent, and its
      // presence blocks the legacy migration -- see effortToCarry.
      const inFlightEffort = inFlightSlotSwitchOutcome('reasoning_effort', slotKey)
      const carriedEffort = effortToCarry(
        paneSlot?.model || '', paneSlot?.reasoning_effort || '',
        stagedSlotSwitchTarget('reasoning_effort', slotKey),
        inFlightEffort !== null,
        codexPairModels,
      )
      await switchGroupedModel(carriedEffort, async level => {
        let normalizedModel: string | undefined
        await performSlotSwitch('reasoning_effort', slotKey, level,
          async () => {
            const response = await api.chatSlotReasoningEffort(slotKey, level)
            normalizedModel = response?.model
            return response?.reasoning_effort ?? level
          },
          value => dispatch(updateSlot({
            key: slotKey,
            reasoning_effort: value,
            ...(normalizedModel ? { model: normalizedModel } : {}),
          })))
      }, async afterEffort => {
        // performSlotSwitch owns the whole protocol: serialized dispatch,
        // latest-request-wins adjudication, hung-request timeout, and exactly
        // one store write on the authoritative value (#4523) — the pane must
        // not depend on the coalesced slots rebroadcast to see its own pick.
        await performSlotSwitch('model', slotKey, name,
          async () => {
            // Effort first on the wire (see switchGroupedModel); a refused
            // effort aborts this pick before anything is sent.
            await afterEffort
            const r = await api.chatSlotModel(slotKey, name)
            // A pick taken during a response is committed now and applies
            // from the next message; the store write below shows it at once,
            // so this line is what tells the user the running reply keeps its model.
            if (r?.deferred) dispatch(setAgentSwitchNotice(modelSwitchDeferredMessage(r.model ?? name)))
            return r?.model ?? name
          },
          // The routing flag is written from the REQUEST, not from the response's
          // `model`: the gateway resolves the sentinel to `auto`, so the stored
          // model cannot tell a routed pick from a plain Auto one. Written on
          // every pick, because picking a concrete model is what clears it.
          (value) => dispatch(updateSlot({
            key: slotKey,
            model: value,
            jev_route: name === JEV_ROUTE_MODEL,
          })))
      }, () => inFlightSlotSwitchOutcome('reasoning_effort', slotKey))
      queryClient.invalidateQueries({ queryKey: ['slot-selection-capabilities', slotKey] })
    } catch (e) {
      // Same failure surface as switchAgent above: the shared notice toast,
      // plus the in-pane notice (the toast alone would be the only report of
      // a write that did not persist).
      const notice = agentSwitchErrorNotice(e)
      dispatch(setAgentSwitchNotice(notice))
      setSwitchError(notice.message)
      // Keep the rejected backend value available in developer diagnostics.
      // eslint-disable-next-line no-console
      console.error('[ChatPane] switchModel failed', e)
    }
  }, [codexPairModels, dispatch, paneSlot?.model, paneSlot?.reasoning_effort, queryClient, slotKey])

  // Roving-focus keyboard nav for the pickers (mirrors ChatPage / StyledSelect):
  // ArrowUp/Down across options, Enter/Space select, Escape/Tab close + return
  // focus. AgentDropdownList / ModelDropdownList options already carry
  // role="option" + tabIndex={-1}.
  const { onListKeyDown: onAgentListKeyDown } = useListboxKeyboard({
    open: agentDD.open,
    dropdownRef: agentDD.dropdownRef,
    inputRef: agentDD.inputRef,
    hasFilterInput: true,
    filteredCount: agentDD.filtered.length,
    onEnterSingleMatch: () => { switchAgent(agentDD.filtered[0].name, agentDD.filtered[0].selection_kind); agentDD.setOpen(false) },
    closeToTrigger: () => agentDD.setOpen(false),
  })
  const { onListKeyDown: onModelListKeyDown } = useListboxKeyboard({
    open: modelDD.open,
    dropdownRef: modelDD.dropdownRef,
    inputRef: modelDD.inputRef,
    hasFilterInput: true,
    filteredCount: modelDD.filtered.length,
    onEnterSingleMatch: () => { switchModel(modelDD.filtered[0].name); modelDD.setOpen(false) },
    closeToTrigger: () => modelDD.setOpen(false),
  })
  // The pane's model picker embeds the reasoning-effort slider below the list
  // (one control, see ModelEffortDropdown and
  // docs/decisions/2026-06-14-chat-composer-model-and-effort-are-one-control.md).
  // Keys route through the same helper as ModelEffortDropdown; the IME guard
  // claims a Tab before the helper re-aims it into the embedded controls.
  const modelDialogIme = useImeGuard()

  return { switchError, setSwitchError, switchAgent, switchModel, onAgentListKeyDown, onModelListKeyDown, modelDialogIme }
}
