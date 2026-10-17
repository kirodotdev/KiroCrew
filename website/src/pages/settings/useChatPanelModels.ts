/**
 * Settings ▸ Chat ▸ Model's state: the optimistic config-PATCH options its selectors
 * share, the throttle and refusal fallback models, the chat default model and effort,
 * the selectable-models list, the agent-pin notice and the per-role model and effort
 * rows. `ChatPanel` calls `useChatPanelFallbackModels` and `useChatPanelModelDefaults`
 * once per render, on either side of `useChatPanelTypedFields`.
 */
import { useQuery, useMutation, type QueryClient } from '@tanstack/react-query'
import type { DashboardConfig } from '../chat/ChatSettings'
import { api } from '../../api/client'
import { setConfigPathValue, type useOptimisticConfigPaths } from './useOptimisticConfigPaths'
import { useAvailableModelsQuery } from '../../hooks/useAvailableModels'
import { EFFORT_LEVELS, effortLabel, modelSupportsEffort } from '../../lib/effort'
import { normalizeModelKey } from '../../lib/model'
import { fmtNumber } from '../../i18n/format'
import { normalizeHiddenModels } from '../../hooks/useInteractiveModels'
import { i18nT } from '../../i18n/t'
import type { KiroCrewAgent } from '../../components/AgentSelector'
import type {
  KirocrewConfigShape,
} from './chatPanelOptions'

type OptimisticOverlay = ReturnType<typeof useOptimisticConfigPaths>

  /**
   * Mutation options for a config PATCH with an OPTIMISTIC display: the seven
   * Model-section selectors render `shown(path, server)`, so a pick shows
   * immediately instead of waiting for the PATCH + refetch round-trip.
   * Lifecycle (per-path pending entry, monotonic ownership token,
   * token-guarded success cache write, error-path refetch) lives in
   * useOptimisticConfigPaths — this factory only binds the panel's query
   * key, PATCH call, and path-scoped failure banner. `''` is a meaningful
   * value here ("model default" / fallback "disabled"); the overlay's
   * explicit entry check preserves it.
   *
   * The failure banner is token-guarded by the hook: a pick superseded by a
   * newer pick on the same path reports nothing when it eventually fails —
   * the newer pick owns the display, and a stale "failed to save" beside a
   * value that did persist is exactly the co-render this prevents.
   */
export function chatPanelConfigOpts(
  overlay: OptimisticOverlay,
  setPathSaveError: (path: string, msg: string) => void,
  clearOwnPathError: (path: string) => void,
) {
  return (path: string, errMsg: (err: unknown) => string) =>
    overlay.mutationOpts<string>({
      queryKey: ['kirocrewConfig'],
      mutationFn: (v: string) => api.patchConfig(path, v),
      path: () => path,
      displayValue: v => v,
      applyToCache: (cached, v) => setConfigPathValue(cached as KirocrewConfigShape, path, v),
      onFailure: err => setPathSaveError(path, errMsg(err)),
      onSupersede: clearOwnPathError,
    })
}

export type ChatPanelConfigOpts = ReturnType<typeof chatPanelConfigOpts>

type HiddenModelsUpdate = {
  next: string[]
  add?: string[]
  remove?: string[]
}

export function useChatPanelFallbackModels({
  overlay, mcCfg, optimisticConfigOpts,
}: {
  overlay: OptimisticOverlay
  mcCfg: KirocrewConfigShape | undefined
  optimisticConfigOpts: ChatPanelConfigOpts
}) {
  // ── Throttle-fallback model (agent.fallback_model) ──
  // Single-select dropdown fed by the same advertised-model list as the
  // role-model rows (no free text — a typo'd id can't exist). "" = disabled,
  // "auto" (default) = backend availability-aware routing, concrete id =
  // tried first with "auto" as the final fallthrough.
  const fallbackModel = mcCfg?.agent?.fallback_model ?? 'auto'
  const shownFallbackModel = overlay.shown('agent.fallback_model', fallbackModel)
  const fallbackMut = useMutation(
    optimisticConfigOpts('agent.fallback_model', (err: unknown) => {
      // Surface the backend's actual deny reason (e.g. an unentitled id)
      // next to the generic failure line.
      const reason = err instanceof Error && err.message ? `: ${err.message}` : ''
      return i18nT('pages.settings.chatPanel.failed_to_save_fallback_model') + reason
    })
  )
  const fallbackModelLabels = (opts: string[]): string[] =>
    opts.map(m =>
      m === ''
        ? i18nT('pages.settings.chatPanel.fallback_disabled')
        : m === 'auto'
          ? i18nT('pages.settings.chatPanel.fallback_auto')
          : m,
    )

  // ── Content-filter (refusal) fallback model (agent.refusal_fallback_model) ──
  // Same advertised-model dropdown as the throttle fallback above. "" =
  // disabled (default — a refusal surfaces exactly as before), "auto" = retry
  // on the model the provider's refusal envelope recommends (when it names
  // one), concrete id = retry the declined message once on it; the primary
  // model is restored on the next message either way.
  const refusalFallbackModel = mcCfg?.agent?.refusal_fallback_model ?? ''
  const shownRefusalFallbackModel = overlay.shown('agent.refusal_fallback_model', refusalFallbackModel)
  const refusalFallbackMut = useMutation(
    optimisticConfigOpts('agent.refusal_fallback_model', (err: unknown) => {
      // Surface the backend's actual deny reason (e.g. an unentitled id)
      // next to the generic failure line.
      const reason = err instanceof Error && err.message ? `: ${err.message}` : ''
      return i18nT('pages.settings.chatPanel.failed_to_save_refusal_fallback_model') + reason
    })
  )
  const refusalFallbackModelLabels = (opts: string[]): string[] =>
    opts.map(m =>
      m === ''
        ? i18nT('pages.settings.chatPanel.fallback_disabled')
        : m === 'auto'
          ? i18nT('pages.settings.chatPanel.refusal_fallback_auto')
          : m,
    )
  return {
    fallbackModel, shownFallbackModel, fallbackMut, fallbackModelLabels, refusalFallbackModel,
    shownRefusalFallbackModel, refusalFallbackMut, refusalFallbackModelLabels,
  }
}

export function useChatPanelModelDefaults({
  qc, setPathSaveError, clearOwnPathError, overlay, dashCfg, mcCfg, optimisticConfigOpts,
  fallbackModel, shownFallbackModel, refusalFallbackModel, shownRefusalFallbackModel,
}: {
  qc: QueryClient
  setPathSaveError: (path: string, msg: string) => void
  clearOwnPathError: (path: string) => void
  overlay: OptimisticOverlay
  dashCfg: DashboardConfig
  mcCfg: KirocrewConfigShape | undefined
  optimisticConfigOpts: ChatPanelConfigOpts
  fallbackModel: string
  shownFallbackModel: string
  refusalFallbackModel: string
  shownRefusalFallbackModel: string
}) {
  // ── Default model + default reasoning effort ──
  // These are the DEFAULTS for new sessions. A session's own model/effort
  // picker still overrides them per-slot; nothing here touches live sessions.
  // Same query key as every other model picker so the list is fetched once.
  const availableModelsQ = useAvailableModelsQuery()
  const availableModels = availableModelsQ.data
  const hiddenModels = overlay.shown(
    'dashboard.model_picker_hidden_models',
    normalizeHiddenModels(dashCfg.model_picker_hidden_models),
  )
  const hiddenModelSet = new Set(hiddenModels)
  const selectedModelIds = new Set(
    availableModels
      .filter(model => model.name === 'auto' || !hiddenModelSet.has(model.name))
      .map(model => model.name),
  )
  const selectedModelCount = selectedModelIds.size
  const modelPickerSummary = selectedModelCount === availableModels.length
    ? i18nT('pages.settings.chatPanel.model_picker_all_models', { count: fmtNumber(availableModels.length) })
    : i18nT('pages.settings.chatPanel.model_picker_selected_models', {
        selected: fmtNumber(selectedModelCount),
        total: fmtNumber(availableModels.length),
      })
  const hiddenModelsOpts = overlay.mutationOpts<HiddenModelsUpdate>({
      queryKey: ['dashboardConfig'],
      mutationFn: ({ add, remove }: HiddenModelsUpdate) => api.updateDashboardConfig({
        ...(add ? { model_picker_hidden_models_add: add } : {}),
        ...(remove ? { model_picker_hidden_models_remove: remove } : {}),
      }),
      path: () => 'dashboard.model_picker_hidden_models',
      displayValue: update => update.next,
      applyToCache: (cached, update) => ({
        ...(cached as DashboardConfig),
        model_picker_hidden_models: update.next,
        model_picker_configured: true,
      }),
      onFailure: () => {
        setPathSaveError(
          'dashboard.model_picker_hidden_models',
          i18nT('pages.settings.chatPanel.failed_to_save_selectable_models'),
        )
      },
      onSupersede: clearOwnPathError,
    })
  const hiddenModelsMut = useMutation({
    ...hiddenModelsOpts,
    onSuccess: (data, update, token) => {
      // This acknowledgement is monotonic even if a newer list edit superseded
      // the successful save. Never mark a visit, pending write, or failure.
      qc.setQueryData<DashboardConfig>(['dashboardConfig'], cached => cached
        ? { ...cached, model_picker_configured: true }
        : cached)
      return hiddenModelsOpts.onSuccess(data, update, token)
    },
    // Serializing this path keeps this tab's delta sequence in UI order while the
    // server applies each delta against the current config under its write lock.
    scope: { id: 'dashboard.model_picker_hidden_models' },
  })
  const saveHiddenModels = (update: HiddenModelsUpdate) => {
    hiddenModelsMut.mutate(update)
  }
  const toggleVisibleModel = (model: string, selected: boolean) => {
    if (model === 'auto') return
    const next = selected
      ? hiddenModels.filter(value => value !== model)
      : [...hiddenModels.filter(value => value !== model), model]
    saveHiddenModels(selected ? { next, remove: [model] } : { next, add: [model] })
  }
  const advertisedModelIds = new Set(availableModels.map(model => model.name))
  const hiddenUnadvertisedModels = hiddenModels.filter(model => !advertisedModelIds.has(model))
  const advertisedOptionalModelIds = availableModels.filter(model => model.name !== 'auto').map(model => model.name)
  const selectAllModels = () => saveHiddenModels({
    next: hiddenUnadvertisedModels,
    remove: advertisedOptionalModelIds,
  })
  const deselectAllModels = () => saveHiddenModels({
    next: [...hiddenUnadvertisedModels, ...advertisedOptionalModelIds],
    add: advertisedOptionalModelIds,
  })
  // '' in config means "unset" and resolves the same way 'auto' does, so both
  // render as the 'auto' option rather than as a missing selection.
  const defaultModel = mcCfg?.agent?.model || 'auto'
  const shownDefaultModel = overlay.shown('agent.model', defaultModel)
  const modelOptions = availableModels.map(m => m.name)
  // A model the live backend no longer advertises must still be selectable,
  // otherwise the select would silently jump to another entry and a stray
  // change event would overwrite the user's stored choice. Both the SHOWN and
  // the persisted value are kept: while a pick is in flight the server's
  // unadvertised id must not vanish from the list, or the user could not
  // change back to it during that window.
  for (const kept of [defaultModel, shownDefaultModel]) {
    if (!modelOptions.includes(kept)) modelOptions.unshift(kept)
  }

  const defaultModelOpts = optimisticConfigOpts('agent.model', () => i18nT('pages.settings.chatPanel.failed_to_save_default_model'))
  const defaultModelMut = useMutation({
    ...defaultModelOpts,
    // The default agent's resolved model falls back to `agent.model`, so the
    // pin notice below must re-ask the resolver once the new global lands.
    onSuccess: (data: unknown, v: string, token: number) => {
      qc.invalidateQueries({ queryKey: ['resolved-model'] })
      return defaultModelOpts.onSuccess(data, v, token)
    },
  })

  // A new chat on the default agent does not necessarily start on `agent.model`:
  // the agent's own pin, then its template's pin, outrank it. The backend owns
  // that precedence (`GET /api/agents/resolved-model`, the same resolver every
  // new session runs through), so the notice below compares its answer with
  // this select rather than re-deriving the chain from the roster — which would
  // miss a template pin and would notice a pin the active harness cannot claim.
  const agentsQ = useQuery<{ agents?: KiroCrewAgent[]; default_agent?: string }>({
    queryKey: ['kirocrew-agents'],
    queryFn: () => api.kirocrewAgents(),
  })
  const pinAgentName = agentsQ.data?.default_agent || 'default'
  const pinAgent = agentsQ.data?.agents?.find(a => a.name === pinAgentName)
  // Asked BY NAME, for the same agent the clear button below writes to, and
  // only once the roster has named it. An unnamed ask ("the server's default
  // agent") can answer for a different agent than a roster read that has not
  // caught up with a default-agent change, and the button would then clear
  // the wrong agent's pin.
  const resolvedQ = useQuery<{ model?: string; pinned?: boolean; reasoning_effort?: string; effort_pinned?: boolean }>({
    queryKey: ['resolved-model', pinAgentName],
    queryFn: () => api.agentResolvedModel(pinAgentName),
    enabled: agentsQ.isSuccess,
  })
  // Either read in flight means the name or the verdict may be about to change
  // under the button, so it waits. So does a global-default save still in
  // flight: clearing the pin then would erase it before that save is known to
  // land, and a rejected save would leave the agent on neither model.
  const pinReadsInFlight = agentsQ.isFetching || resolvedQ.isFetching
  // A failed re-read keeps the previous answer on screen, and that answer may
  // no longer be true (the pin changed elsewhere), so the button waits for a
  // good read too. The banner above names the failure and offers Retry.
  const pinReadsFailed = agentsQ.isError || resolvedQ.isError
  // Compared as canonical keys, not raw ids: a pin spelled `claude-opus-4.8`
  // names the same model as the setting's `claude-opus-4-8[1m]` or `opus`.
  const resolvedModel = resolvedQ.data?.model || ''
  const resolvedKey = normalizeModelKey(resolvedModel)
  const globalKey = normalizeModelKey(defaultModel)
  // Only an explicit global default can be overridden: on Auto the setting
  // itself says the agent config decides, so whatever resolves is expected. The
  // SHOWN value is also accepted so a pick still in flight does not flash a
  // notice against the answer the server has not recomputed yet.
  const agentPinOverrides =
    globalKey !== 'auto' &&
    !!resolvedKey && resolvedKey !== 'auto' &&
    resolvedKey !== globalKey &&
    resolvedKey !== normalizeModelKey(shownDefaultModel)
  // Which tier answered. `pinned` is the resolver's own word for "this agent's
  // record carries a model pin" (the agent's own pin, never its template's);
  // a resolved model without one came from a tier this panel cannot edit (the
  // template, or the backend default when the global model is out of the
  // agent's scope) — so that notice has no button and points at the chat model
  // picker instead.
  //
  // `pinned` only says the record CARRIES a pin, not that the pin won: a pin
  // the active backend cannot use is skipped by the resolver and kept on the
  // record for when that backend returns. So the member wording, and the button
  // that erases the pin, also require the resolved model to BE that pin. A pin
  // that is carried but skipped belongs to another backend, which this notice
  // does not cover, so it shows nothing rather than a wrong source.
  const memberPinCarried = resolvedQ.data?.pinned === true
  const memberPinApplied = memberPinCarried && !!pinAgent?.model &&
    normalizeModelKey(pinAgent.model) === resolvedKey
  const memberPinOverrides = agentPinOverrides && memberPinApplied
  const showPinNotice = agentPinOverrides && (!memberPinCarried || memberPinApplied)
  const clearAgentPinMut = useMutation({
    // '' is the inherit sentinel: the agent falls back to the global default.
    mutationFn: () => api.updateKirocrewAgent(pinAgentName, { model: '' }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['kirocrew-agents'] })
      qc.invalidateQueries({ queryKey: ['resolved-model'] })
    },
    // No onError banner: the failure renders inline beside the button that
    // caused it (`clearAgentPinMut.error` below), not at the top of the panel.
  })
  const clearAgentPinError = clearAgentPinMut.isError
    ? (clearAgentPinMut.error instanceof Error && clearAgentPinMut.error.message
      ? i18nT('pages.settings.chatPanel.failed_to_clear_agent_model_pin', { error: clearAgentPinMut.error.message })
      : i18nT('pages.settings.chatPanel.failed_to_clear_agent_model_pin_no_reason'))
    : ''

  const defaultEffort = mcCfg?.agent?.reasoning_effort ?? ''
  const shownDefaultEffort = overlay.shown('agent.reasoning_effort', defaultEffort)
  // Effort is only meaningful on reasoning-capable models. Rather than hide the
  // row (which would make the setting look absent), keep it visible and
  // disabled with an explanatory hint. Gated on the SHOWN model so the row's
  // enabled state tracks the trigger the user is looking at, not a value the
  // refetch has yet to replace.
  const effortSupported = modelSupportsEffort(shownDefaultModel)
  const defaultEffortOpts = optimisticConfigOpts('agent.reasoning_effort', () =>
    i18nT('pages.settings.chatPanel.failed_to_save_default_reasoning_effort')
  )
  const defaultEffortMut = useMutation({
    ...defaultEffortOpts,
    // An agent with no effort pin resolves to `agent.reasoning_effort`, so the
    // effort notice below re-asks the resolver once the new global lands.
    onSuccess: (data: unknown, v: string, token: number) => {
      qc.invalidateQueries({ queryKey: ['resolved-model'] })
      return defaultEffortOpts.onSuccess(data, v, token)
    },
  })

  // ── Default agent's effort pin vs Default Reasoning Effort ──
  // Same question as the model notice above, asked of the same resolver read:
  // `reasoning_effort` is what a new chat on the default agent starts at
  // (`KiroCrewConfig.resolve_session_effort`: the agent's own pin, then the
  // role-aware default), and `effort_pinned` says the agent's own record won.
  // A gateway that predates those fields leaves them unset, which shows nothing.
  const resolvedEffortRaw = resolvedQ.data?.reasoning_effort
  const resolvedEffort = typeof resolvedEffortRaw === 'string' ? resolvedEffortRaw.trim().toLowerCase() : null
  // '' is a real choice here ("Model default"), not Auto: it promises the model's
  // own effort, so a pin that replaces it is an override worth naming too. The
  // SHOWN value is accepted for the same in-flight reason as the model notice.
  // Effort only matters on a model that takes one, so a chat that starts on a
  // model without it (or on the backend's own choice) shows nothing.
  const agentEffortOverrides =
    resolvedEffort !== null &&
    modelSupportsEffort(resolvedModel) &&
    resolvedEffort !== defaultEffort.trim().toLowerCase() &&
    resolvedEffort !== shownDefaultEffort.trim().toLowerCase()
  // The clear button erases the roster's pin, so it also requires the roster to
  // carry the pin the resolver answered with — the same cross-check the model
  // notice makes before it offers to erase anything.
  const memberEffortPinOverrides = agentEffortOverrides &&
    resolvedQ.data?.effort_pinned === true &&
    (pinAgent?.reasoning_effort ?? '').trim().toLowerCase() === resolvedEffort
  // Without the agent's own pin, the only other tier is the Background role
  // effort (an agent bound to a background worker takes it), which this page
  // sets a few rows down — so that notice names it and has no button. It shows
  // only when the answer IS that row's value, saved or shown, so the notice
  // never names Background Effort as the source of an effort it did not set.
  const savedBgEffort = mcCfg?.agent?.role_efforts?.background ?? ''
  const roleEffortAnswered = [savedBgEffort, overlay.shown('agent.role_efforts.background', savedBgEffort)]
    .some(v => v.trim().toLowerCase() === resolvedEffort)
  const showEffortPinNotice = agentEffortOverrides &&
    (memberEffortPinOverrides || (resolvedQ.data?.effort_pinned !== true && roleEffortAnswered))
  const resolvedEffortLabel = resolvedEffort
    ? effortLabel(resolvedEffort)
    : i18nT('pages.settings.chatPanel.model_default')
  const clearAgentEffortPinMut = useMutation({
    // '' is the inherit sentinel: the agent falls back to the global default.
    mutationFn: () => api.updateKirocrewAgent(pinAgentName, { reasoning_effort: '' }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['kirocrew-agents'] })
      qc.invalidateQueries({ queryKey: ['resolved-model'] })
    },
    // Rendered inline beside the button, like the model clear above.
  })
  const clearAgentEffortPinError = clearAgentEffortPinMut.isError
    ? (clearAgentEffortPinMut.error instanceof Error && clearAgentEffortPinMut.error.message
      ? i18nT('pages.settings.chatPanel.failed_to_clear_agent_effort_pin', { error: clearAgentEffortPinMut.error.message })
      : i18nT('pages.settings.chatPanel.failed_to_clear_agent_effort_pin_no_reason'))
    : ''

  // ── Per-role model defaults (agent.role_models) ──
  // Same picker as the chat default above, but NOT the same precedence:
  // `RoleModels.resolve_model` returns the role's own pin or "auto" and
  // deliberately never falls back to `agent.model`, so unattended work cannot
  // silently ride the interactive flagship on every cycle. "auto" therefore
  // means "the provider picks", not "inherit the chat default" — which is why
  // these rows label it differently from the chat row's Default (auto).
  const backgroundModel = mcCfg?.agent?.role_models?.background || 'auto'
  const subagentModel = mcCfg?.agent?.role_models?.subagent || 'auto'
  const shownBackgroundModel = overlay.shown('agent.role_models.background', backgroundModel)
  const shownSubagentModel = overlay.shown('agent.role_models.subagent', subagentModel)
  // A pinned model the live backend no longer advertises must stay selectable
  // (same reasoning as the chat-default picker), so prepend what is missing —
  // both the shown value and the persisted one, so neither vanishes while a
  // pick is in flight.
  const roleModelOptions = (shown: string, server: string): string[] => {
    const opts = availableModels.map(m => m.name)
    for (const kept of [server, shown]) {
      if (!opts.includes(kept)) opts.unshift(kept)
    }
    return opts
  }
  const roleModelLabels = (opts: string[]): string[] =>
    opts.map(m => (m === 'auto' ? i18nT('pages.settings.chatPanel.role_model_auto') : m))
  const fallbackModelOptions = (shown: string, server: string): string[] => {
    const opts = ['', 'auto', ...availableModels.map(m => m.name).filter(m => m !== 'auto')]
    // Keep both the shown and the persisted id selectable while they differ:
    // an in-flight pick must not drop the server's unadvertised id from the
    // list, or the user could not switch back to it during that window.
    for (const kept of [server, shown]) {
      if (kept && !opts.includes(kept)) opts.splice(2, 0, kept)
    }
    return opts
  }
  // One array per row, shared by `options` and `optionLabels`: SettingsSelect
  // pairs a label to a value by INDEX, so both props must read the same list.
  const backgroundModelOpts = roleModelOptions(shownBackgroundModel, backgroundModel)
  const subagentModelOpts = roleModelOptions(shownSubagentModel, subagentModel)
  const fallbackOpts = fallbackModelOptions(shownFallbackModel, fallbackModel)
  const refusalFallbackOpts = fallbackModelOptions(shownRefusalFallbackModel, refusalFallbackModel)
  const backgroundModelMut = useMutation(
    optimisticConfigOpts('agent.role_models.background', () => i18nT('pages.settings.chatPanel.failed_to_save_role_model'))
  )
  const subagentModelMut = useMutation(
    optimisticConfigOpts('agent.role_models.subagent', () => i18nT('pages.settings.chatPanel.failed_to_save_role_model'))
  )

  // Per-role reasoning effort, paired with each role's model. Empty inherits the
  // the MODEL's own default: `RoleModels.resolve_effort` does not fall back to
  // `agent.reasoning_effort` either. The effort row is only meaningful on a
  // reasoning-capable model, so it disables against a resolved model.
  //
  // KNOWN GAP: the two gates below resolve `auto` to the CHAT default, which
  // `resolve_model` never does — so a role on auto can offer an effort control
  // for a model that role will not run on. Changing it is a behaviour change
  // with a test asserting the current answer, so it is tracked separately
  // rather than folded into this copy fix.
  const backgroundEffort = mcCfg?.agent?.role_efforts?.background ?? ''
  const subagentEffort = mcCfg?.agent?.role_efforts?.subagent ?? ''
  const shownBackgroundEffort = overlay.shown('agent.role_efforts.background', backgroundEffort)
  const shownSubagentEffort = overlay.shown('agent.role_efforts.subagent', subagentEffort)
  const bgEffortSupported = modelSupportsEffort(shownBackgroundModel !== 'auto' ? shownBackgroundModel : shownDefaultModel)
  const subEffortSupported = modelSupportsEffort(shownSubagentModel !== 'auto' ? shownSubagentModel : shownDefaultModel)
  const effortLabels = EFFORT_LEVELS.map(l => (l === '' ? i18nT('pages.settings.chatPanel.model_default') : effortLabel(l)))
  const backgroundEffortOpts = optimisticConfigOpts(
    'agent.role_efforts.background', () => i18nT('pages.settings.chatPanel.failed_to_save_role_effort'),
  )
  const backgroundEffortMut = useMutation({
    ...backgroundEffortOpts,
    // A default agent bound to a background worker resolves to this effort, so
    // the effort notice above names it and must re-ask once it changes.
    onSuccess: (data: unknown, v: string, token: number) => {
      qc.invalidateQueries({ queryKey: ['resolved-model'] })
      return backgroundEffortOpts.onSuccess(data, v, token)
    },
  })
  const subagentEffortMut = useMutation(
    optimisticConfigOpts('agent.role_efforts.subagent', () => i18nT('pages.settings.chatPanel.failed_to_save_role_effort'))
  )
  return {
    availableModelsQ, availableModels, selectedModelIds, modelPickerSummary, toggleVisibleModel,
    selectAllModels, deselectAllModels, shownDefaultModel, modelOptions, defaultModelMut, agentsQ,
    pinAgentName, pinAgent, resolvedQ, pinReadsInFlight, pinReadsFailed, resolvedModel,
    memberPinOverrides, showPinNotice, clearAgentPinMut, clearAgentPinError, shownDefaultEffort,
    effortSupported, defaultEffortMut, shownBackgroundModel, shownSubagentModel, roleModelLabels,
    backgroundModelOpts, subagentModelOpts, fallbackOpts, refusalFallbackOpts, backgroundModelMut,
    subagentModelMut, shownBackgroundEffort, shownSubagentEffort, bgEffortSupported,
    subEffortSupported, effortLabels, backgroundEffortMut, subagentEffortMut,
    memberEffortPinOverrides, showEffortPinNotice, resolvedEffortLabel, clearAgentEffortPinMut,
    clearAgentEffortPinError,
  }
}
