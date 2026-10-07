import { useMemo } from 'react'
import { useMutation, useQuery, type QueryClient } from '@tanstack/react-query'

import { api } from '../../../api/client'
import type { KiroCrewAgent } from '../../../components/AgentSelector'
import { filterInteractiveModels, legacyCodexEffort, modelWithoutEffort } from '../../../hooks/useInteractiveModels'
import { useKirocrewConfigReader } from '../../../hooks/useKirocrewConfigReader'
import { useSettingsDefaultModel } from '../../../hooks/useSettingsDefaultModel'
import type { useRemoteCapabilities } from '../../../hooks/useRemoteCapabilities'
import { i18nT } from '../../../i18n/t'
import { modelSupportsEffort, selectionCapabilitiesFailed } from '../../../lib/effort'
import { displayModel, modelChipMarker } from '../../../lib/model'
import type { useProvider } from '../../../providers'
import { modelHealthKey, useModelsDegraded } from '../../../providers/modelListHealth'
import type { ModelInfo } from '../../../providers/types'
import type { AppDispatch } from '../../../store'
import { triggerRefresh } from '../../../store/dashboardSlice'
import { addNotification } from '../../../store/notificationsSlice'
import type { ChatSlot } from '../../../types'
import { uniqueNotificationTs } from './notificationTs'

interface ComposerChipsOptions {
  currentSlot: ChatSlot | undefined
  /** The configured default agent and the one a new session would open on. */
  defaultAgent: string | undefined
  pendingAgent: string
  installedAgents: KiroCrewAgent[]
  provider: ReturnType<typeof useProvider>
  /** The roster the picker offers (the peer's for a remote-bound session). */
  availableModels: ModelInfo[]
  codexPairModels: boolean
  /** The slot's ACP capability answer, once known. */
  selectionCapabilities: Awaited<ReturnType<typeof api.chatSlotSelectionCapabilities>> | undefined
  selectionCapabilitiesQ: {
    isError: boolean
    error?: unknown
    data?: { models_backend?: string; effort_supported?: boolean | null; effort_levels?: string[] }
  }
  remoteCrew: ReturnType<typeof useRemoteCapabilities>
  dispatch: AppDispatch
  queryClient: QueryClient
  showActionError: (message: string, title?: string) => void
}

/**
 * What the composer's model, effort and project chips show for the active
 * slot -- the model to display (never a withheld pin), whether effort applies
 * and at what level, the inherited default effort, the project's branch --
 * and the model picker's "set as the agent's default" write.
 */
export function useComposerChips({
  currentSlot,
  defaultAgent,
  pendingAgent,
  installedAgents,
  provider,
  availableModels,
  codexPairModels,
  selectionCapabilities,
  selectionCapabilitiesQ,
  remoteCrew,
  dispatch,
  queryClient,
  showActionError,
}: ComposerChipsOptions) {
  // Resolve model for existing slots that don't have one stored
  const _slotAgentName = (currentSlot && !currentSlot.model) ? (currentSlot.agent || defaultAgent || 'default') : ''
  const { data: _slotResolvedModel } = useQuery({
    queryKey: ['resolved-model', _slotAgentName, provider.id],
    queryFn: () => provider.resolveModel(_slotAgentName),
    enabled: !!_slotAgentName,
  })
  // The agent the composer's "set as default" row acts on: the active slot's
  // agent, else whichever agent a new session would open on.
  const _modelPinAgent = currentSlot?.agent || pendingAgent || defaultAgent || 'default'
  const _modelPinCfg = installedAgents.find(a => a.name === _modelPinAgent)
  // Writes agents.<name>.model in config.json. Invalidates the resolved-model
  // queries so a slot showing an inherited value picks the new pin up without a
  // reload; open sessions keep the model they already resolved.
  const pinModelToAgentMut = useMutation({
    mutationFn: ({ agent, model }: { agent: string; model: string }) =>
      api.updateKirocrewAgent(agent, { model }),
    onSuccess: () => {
      dispatch(triggerRefresh())
      queryClient.invalidateQueries({ queryKey: ['resolved-model'] })
    },
    // The dropdown closes as soon as the row is clicked, so without this a
    // failed write left NOTHING on screen and the old default silently stood —
    // discoverable only by reopening the menu. Body is the agent name plus the
    // server's own message, so it carries no untranslated prose of its own.
    onError: (e: Error, vars) => {
      const title = i18nT('pages.chatPage.could_not_set_the_agent_default_model')
      const body = `${vars.agent}: ${e?.message || i18nT('components.errorBoundary.something_went_wrong')}`
      // The save did not persist, so the page itself has to say so — the toast
      // is transient and lives in the notification centre.
      showActionError(body, title)
      dispatch(addNotification({
        ts: uniqueNotificationTs(),
        kind: 'agent',
        priority: 'critical',
        title,
        body,
      }))
    },
  })
  // Derived, not mirrored into state via an effect: the effect form cost an extra
  // render pass every time the query settled, for a value that is a pure function
  // of the query result.
  const resolvedModel = _slotResolvedModel || ''
  // The model to DISPLAY for this slot. A slot can stay pinned to a model the
  // account can no longer run (a plan downgrade leaves the pin behind): the
  // backend withholds it at spawn and runs the session on its own default, so
  // showing the pin would name a model no turn will use. The slot carries the
  // backend's verdict for exactly that (`model_withheld`), and it is pinned
  // server-side to the model it was computed for, so it always describes the
  // first operand below whenever that operand is the slot's own pin. The
  // degraded flag gates the list-membership fallback used when there is no
  // verdict — a cached list served while /api/models fails is stale, not
  // authoritative — and is subscribed to rather than read, because it can flip
  // without the list changing.
  // The flag of the list the chip reads: a session on another harness reads that harness's own list.
  const _modelsDegraded = useModelsDegraded(modelHealthKey(provider.id, selectionCapabilitiesQ.data?.models_backend))
  const displayModels = codexPairModels
    ? filterInteractiveModels(availableModels, [], [], true)
    : availableModels
  const modelPin = currentSlot?.model || resolvedModel || ''
  const displayPin = codexPairModels ? modelWithoutEffort(modelPin) : modelPin
  const shownModel = displayModel(
    displayPin,
    displayModels,
    _modelsDegraded,
    currentSlot?.model_withheld,
    // Names the backend's own choice when the slot inherits, so the chip is not
    // a bare `auto` for a session running one specific model.
    codexPairModels ? modelWithoutEffort(currentSlot?.served_model || '') : currentSlot?.served_model,
  )
  // Before a session reports, its own harness answers when the server gave one. A null answer is
  // "no session on that harness has said yet", so the model answers then, as in the crew editor.
  const effortCaps = selectionCapabilities
    ?? (selectionCapabilitiesQ.data?.effort_supported == null ? undefined : selectionCapabilitiesQ.data)
  const effortSupported = provider.capabilities.reasoningEffort && !selectionCapabilitiesFailed(selectionCapabilitiesQ) && (
    effortCaps
      ? effortCaps.effort_supported === true
      : modelSupportsEffort(shownModel === 'auto' ? '' : shownModel)
  )
  const effortLevelsOverride = effortCaps
    ? effortCaps.effort_levels
    : remoteCrew.isRemote ? (remoteCrew.capabilities?.effort_levels ?? []) : undefined
  // The same answer WITHOUT that substitution, for the pin-to-agent row: that
  // row asks about the PIN, and it must stay disabled for a withheld one even
  // now that the chip names the model the session inherited instead.
  const _pinShownModel = displayModel(
    displayPin,
    displayModels,
    _modelsDegraded,
    currentSlot?.model_withheld,
  )
  // The chip says `default` only for the Settings default; a model picked for
  // the user (Auto router, withheld pin's fallback) is marked `auto` instead.
  const chipDefault = useSettingsDefaultModel(_slotAgentName, remoteCrew.isRemote, codexPairModels)
  const modelMarker = modelChipMarker(
    currentSlot?.model || '',
    shownModel,
    _pinShownModel,
    chipDefault.settingsDefault,
    chipDefault.agentPinned,
  )
  // Context-window fallback for a peer-bound session BEFORE its first turn. Once a
  // turn has run the real number arrives with the relayed `context_usage` frame and
  // wins; until then `provider.getContextWindow` would answer from THIS machine's
  // model knowledge, which can differ from the peer's for the same model name.
  const remoteContextWindow = useMemo(() => {
    if (!remoteCrew.isRemote) return 0
    const picked = shownModel === 'auto' ? '' : shownModel
    return remoteCrew.capabilities?.models.find(m =>
      (codexPairModels ? modelWithoutEffort(m.model_name) : m.model_name) === picked,
    )?.context_window || 0
  }, [remoteCrew.isRemote, remoteCrew.capabilities, shownModel, codexPairModels])
  // True when the pin row would be a no-op: the agent already stores the
  // selected base model. 'auto' is the inherit spelling, never a stored pin.
  // Read the slot's pin through displayPin, not the fallback shownModel: a
  // withheld model must never become the value saved to the agent template.
  const _modelPinActive = displayPin
  const _modelPinPinned =
    !!_modelPinCfg?.model &&
    (codexPairModels ? modelWithoutEffort(_modelPinCfg.model) : _modelPinCfg.model) === _modelPinActive &&
    _modelPinActive !== 'auto'
  // The configured default effort for new sessions. A slot that has never
  // touched the effort control carries '' (no override) but still RUNS at this
  // default — the backend applies `slot.reasoning_effort or agent.reasoning_effort`
  // — so the composer must show the inherited value rather than a bare
  // "Default", which read as "the model decides" and hid the real setting.
  const readKirocrewConfig = useKirocrewConfigReader()
  const { data: _defaultEffort } = useQuery({
    queryKey: ['default-effort', provider.id],
    queryFn: () => provider.resolveDefaultEffort(readKirocrewConfig),
    enabled: provider.capabilities.reasoningEffort,
  })
  const defaultEffort = _defaultEffort || ''
  // Effort actually in force for the active slot: per-slot override, else the
  // configured default. Display only — the slot's raw value still drives the
  // picker so "no override" stays distinguishable from an explicit pick.
  const effectiveEffort = currentSlot?.reasoning_effort || legacyCodexEffort(
    currentSlot?.model || '', '', codexPairModels,
  ) || defaultEffort
  // Branch label for the active project chip. The user can check out a
  // different branch outside the dashboard at any time, so this refetches on a
  // slow interval and on window focus rather than being read once. A failure
  // (no git, path gone, not a repo) leaves the chip showing the folder name
  // alone, which is the pre-existing behaviour.
  const _slotProject = currentSlot?.project || ''
  const { data: projectGit, isError: projectGitError } = useQuery({
    queryKey: ['project-git', _slotProject],
    queryFn: () => api.projectGit(_slotProject),
    enabled: !!_slotProject,
    staleTime: 15_000,
    refetchInterval: 60_000,
    refetchOnWindowFocus: true,
    retry: false,
  })
  // React Query keeps the last successful data after a failed refetch, so a
  // project that was deleted or revoked would keep showing its old branch
  // indefinitely. Treat an errored query as "no branch" and fall back to the
  // folder name, which is the same degradation as a non-repo project.
  const projectBranch = projectGitError
    ? ''
    : projectGit?.branch || (projectGit?.detached ? projectGit.head || '' : '')
  return {
    shownModel, _pinShownModel, chipDefault, modelMarker, effortSupported, effortLevelsOverride, remoteContextWindow,
    _modelPinAgent, _modelPinActive, _modelPinPinned, pinModelToAgentMut,
    defaultEffort, effectiveEffort,
    _slotProject, projectGit, projectGitError, projectBranch,
  }
}
