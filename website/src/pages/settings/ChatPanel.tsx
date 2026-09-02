import { useState, useCallback, useRef, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { SettingsSection, SettingsCard, SettingsToggle, SettingsSelect, SettingsInput, SettingsButtonGroup } from '../../components/settings'
import { loadChatConfig, saveChatConfig, type ChatConfig, type ContentWidth, type DashboardConfig, type SendMode } from '../chat/ChatSettings'
import { api } from '../../api/client'
import { useOptimisticConfigPaths, setConfigPathValue } from './useOptimisticConfigPaths'
import { useAvailableModels } from '../../hooks/useAvailableModels'
import { EFFORT_LEVELS, effortLabel, modelSupportsEffort } from '../../lib/effort'
import { isMac } from '../../utils/platform'
import { capRoleOther, clampRoleOther } from '../../lib/userProfile'
import { ROLE_SLUGS, TECH_SLUGS } from '../../lib/profileOptions'

import { i18nT } from '../../i18n/t'
import ErrorNotice from '../../components/ErrorNotice'
/**
 * Option labels are FUNCTIONS, not module-level arrays.
 *
 * Every `*_LABELS` array below used to be a module-level const, which is evaluated
 * once at import: an `i18nT()` call there would freeze whatever language was active
 * at boot and never re-resolve. Each resolver is called in the render body instead
 * (`optionLabels={roleLabels()}`), so a language switch re-reads the catalog.
 *
 * Each list stays POSITIONALLY paired with its `*_OPTIONS` array — `SettingsSelect`
 * matches a label to a value by index — so entries must be added and reordered in
 * lockstep.
 */
const RESTORE_OPTIONS = ['15', '30', '60', '120', '360', '720', '1440', '0']
/** Duration abbreviations are left verbatim (locale-aware unit formatting is Phase 4
 *  territory); only the `'0'` sentinel's label is prose. It reuses the in-chat
 *  settings popover's key — same setting, same option, one string to translate. */
function restoreLabels(): string[] {
  return ['15m', '30m', '1h', '2h', '6h', '12h', '24h', i18nT('pages.settings.chatPanel.no_limit')]
}
const COMPACT_OPTIONS = ['20', '40', '60', '70', '80', '90']
const COMPACT_LABELS = ['20% (aggressive)', '40%', '60%', '70% (default)', '80%', '90%']

// About You — slugs shared with onboarding step 2 and context.py's prompt maps.
const ROLE_OPTIONS = ['', ...ROLE_SLUGS]
function roleLabels(): string[] {
  return [
    i18nT('pages.settings.chatPanel.not_set'),
    i18nT('pages.settings.chatPanel.developer'),
    i18nT('pages.settings.chatPanel.ux_designer'),
    i18nT('pages.settings.chatPanel.product_manager'),
    i18nT('pages.settings.chatPanel.data_ml'),
    i18nT('pages.settings.chatPanel.it_ops'),
    i18nT('pages.settings.chatPanel.other'),
  ]
}
const TECH_OPTIONS = ['', ...TECH_SLUGS]
function techLabels(): string[] {
  return [
    i18nT('pages.settings.chatPanel.not_set'),
    i18nT('pages.settings.chatPanel.i_write_code'),
    i18nT('pages.settings.chatPanel.somewhat'),
    i18nT('pages.settings.chatPanel.not_technical'),
  ]
}

const SOFT_STOP_MIN = 0.5
const SOFT_STOP_MAX = 60
const SOFT_STOP_DEFAULT = 10.0

type CompletionKeepMode = 'head' | 'tail' | 'both'
const COMPLETION_KEEP_OPTIONS: CompletionKeepMode[] = ['head', 'tail', 'both']

type VerbosityLevel = 'default' | 'concise' | 'ultra' | 'answer_only'
const VERBOSITY_OPTIONS: VerbosityLevel[] = ['default', 'concise', 'ultra', 'answer_only']

/**
 * Narrow a persisted `dashboard.verbosity` to a level this Select can render.
 *
 * The config loader reads the field with a plain `.get()` and does not type-check
 * it, so a hand-edited or migrated `config.json` can put any JSON there — e.g.
 * `{"dashboard": {"verbosity": {}}}` — and the GET response hands that object
 * straight to the UI. `?? 'default'` guards only null/undefined, so an object
 * would flow into SimpleSelect's `triggerFallback`
 * (`optionLabels?.[options.indexOf(value)] ?? (value || '—')`): `indexOf` misses,
 * the object is truthy, and React throws on rendering it as a child — taking the
 * whole Chat settings page down rather than degrading one row.
 */
function asVerbosity(value: unknown): VerbosityLevel {
  return VERBOSITY_OPTIONS.includes(value as VerbosityLevel)
    ? (value as VerbosityLevel)
    : 'default'
}
function completionKeepLabels(): string[] {
  return [
    i18nT('pages.settings.chatPanel.head_preserve_start_of_stream'),
    i18nT('pages.settings.chatPanel.tail_preserve_end_final_summary'),
    i18nT('pages.settings.chatPanel.both_head_tail_with_truncation_marker'),
  ]
}
const COMPLETION_KEEP_CHARS_MIN = 0
// Mirrors RESULT_FILE_MAX_BYTES on the backend (handlers/core.py _EDITABLE_CONFIG).
const COMPLETION_KEEP_CHARS_MAX = 512000
const COMPLETION_KEEP_CHARS_DEFAULT = 3000

/** Shape of the kirocrewConfig query payload this panel reads and patches. */
type KirocrewConfigShape = {
  session?: { autocompact_pct?: number }
  session_summary?: { enabled?: boolean }
  agent?: {
    model?: string
    role_models?: { background?: string; subagent?: string }
    role_efforts?: { background?: string; subagent?: string }
    reasoning_effort?: string
    soft_stop_budget_secs?: number
    completion_keep?: CompletionKeepMode
    completion_keep_chars?: number
    fallback_model?: string
  }
  dashboard?: { user_role?: string; user_role_other?: string; user_technical_level?: string; prevent_sleep?: boolean }
}

export function ChatPanel() {
  const qc = useQueryClient()
  const [chatCfg, setChatCfg] = useState<ChatConfig>(loadChatConfig)
  const [saveError, rawSetSaveError] = useState('')
  // The failure banner is one shared slot written by every save on this panel,
  // so a pick may only auto-clear a failure that came from the SAME picker —
  // its own config path. Clearing more than that (another picker's failure,
  // or a non-picker save's) would dismiss an unresolved error and leave the
  // user believing that setting persisted. The ref records which config path
  // produced the current banner; null = not a picker failure.
  const saveErrorPathRef = useRef<string | null>(null)
  const setSaveError = (msg: string) => {
    saveErrorPathRef.current = null
    rawSetSaveError(msg)
  }
  const setPathSaveError = (path: string, msg: string) => {
    saveErrorPathRef.current = path
    rawSetSaveError(msg)
  }
  // A fresh attempt supersedes a stale failure banner from ITS OWN path:
  // without this a control would show the new value while its own last
  // save's error still hangs above it in the same frame. Failures from any
  // other source — another control included — stay up: this save says
  // nothing about whether that other setting persisted.
  const clearOwnPathError = (path: string) => {
    if (saveErrorPathRef.current === path) setSaveError('')
  }

  // ── Per-path optimistic pending values (shared overlay hook) ──
  // Every optimistic save on this panel renders `shown(path, server)`, so a
  // save displays immediately instead of after its round-trip, and
  // concurrent saves on different paths cannot touch each other's display.
  // Full lifecycle contract: useOptimisticConfigPaths.ts.
  const overlay = useOptimisticConfigPaths(qc)

  // ── Dashboard config (server-side) ──
  const dashQ = useQuery<DashboardConfig>({
    queryKey: ['dashboardConfig'],
    queryFn: () => api.dashboardConfig(),
  })
  // Shown config: the in-flight save when one is pending, else the server's.
  // Toggles both render this and BUILD THEIR PAYLOAD from it (setDash), so a
  // second toggle during a save carries the first one's value forward.
  const dashCfg = overlay.shown(
    'dashboardConfig',
    dashQ.data ?? { restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false, widget_density: 'more' as const, verbosity: 'default' as const, quick_send: false, session_grid: false, tail_fork_enabled: false, link_previews: false, mcp_app_panel: false, auto_open_git_panel: false, session_card_source_links: true, folder_suggestions_enabled: true, use_builtin_browser: true },
  )

  // ── Feature Tips opt-out (server-side per-user state) ──
  const tipsQ = useQuery<{ enabled_config: boolean; opted_out: boolean }>({
    queryKey: ['tipsStatus'],
    queryFn: () => api.tipsStatus(),
  })
  const tipsOpts = overlay.mutationOpts<boolean>({
    queryKey: ['tipsStatus'],
    mutationFn: (enable: boolean) => api.tipsFeedback('', enable ? 'optin' : 'optout'),
    path: () => 'tipsStatus.opted_out',
    displayValue: enable => !enable,
    applyToCache: (cached, enable) => ({ ...(cached as { enabled_config: boolean; opted_out: boolean }), opted_out: !enable }),
    onFailure: () => setPathSaveError('tipsStatus.opted_out', i18nT('pages.settings.chatPanel.failed_to_save_tips_preference')),
    onSupersede: clearOwnPathError,
  })
  const tipsMut = useMutation({
    ...tipsOpts,
    onSettled: (data: unknown, err: unknown, enable: boolean, token: number | undefined) => {
      tipsOpts.onSettled(data, err, enable, token)
      // Drop any cached/in-flight tip so a running Chat view can't display a
      // tip fetched before the preference changed.
      qc.removeQueries({ queryKey: ['tips-next'] })
    },
  })
  const tipsConfigOff = tipsQ.data ? !tipsQ.data.enabled_config : false
  const shownOptedOut = overlay.shown('tipsStatus.opted_out', tipsQ.data?.opted_out)

  // Only the CHANGED keys go on the wire, the way `BrowserPanel`'s own dashboard
  // mutation already does it: the config handler applies whichever keys the body
  // carries, so a full-object PUT rebuilt from this tab's cache would write every
  // OTHER setting back at its cached value -- clobbering one that a second tab
  // (or `kirocrew config set`) changed after we cached it.
  //
  // The overlay still displays and caches the WHOLE object, so the patch is
  // merged in both places. Merging onto the SHOWN config rather than the server
  // value is what keeps the property above -- a second toggle during an in-flight
  // save carries the first one's value forward -- and the monotonic token still
  // keeps a slow earlier save from clobbering a newer one's display or cache write.
  const dashMut = useMutation(overlay.mutationOpts<Partial<DashboardConfig>>({
    queryKey: ['dashboardConfig'],
    mutationFn: (patch: Partial<DashboardConfig>) => api.updateDashboardConfig(patch),
    path: () => 'dashboardConfig',
    displayValue: patch => ({ ...dashCfg, ...patch }),
    applyToCache: (cached, patch) => ({ ...(cached as DashboardConfig), ...patch }),
    onFailure: () => setPathSaveError('dashboardConfig', i18nT('pages.settings.chatPanel.failed_to_save_dashboard_config')),
    onSupersede: clearOwnPathError,
  }))

  // ── KiroCrew config (server-side) ──
  const mcQ = useQuery<KirocrewConfigShape>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  const mcCfg = mcQ.data

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
  const optimisticConfigOpts = (path: string, errMsg: (err: unknown) => string) =>
    overlay.mutationOpts<string>({
      queryKey: ['kirocrewConfig'],
      mutationFn: (v: string) => api.patchConfig(path, v),
      path: () => path,
      displayValue: v => v,
      applyToCache: (cached, v) => setConfigPathValue(cached as KirocrewConfigShape, path, v),
      onFailure: err => setPathSaveError(path, errMsg(err)),
      onSupersede: clearOwnPathError,
    })

  // ── User profile (About You) ──
  // Same slugs as onboarding step 2 (OnboardingFlow.tsx), validated by the
  // config PATCH allowlist (handlers/core.py) and mapped to the prompt's
  // [USER PROFILE] block in context.py.
  const userRole = mcCfg?.dashboard?.user_role ?? ''
  const userRoleOther = mcCfg?.dashboard?.user_role_other ?? ''
  const userTechLevel = mcCfg?.dashboard?.user_technical_level ?? ''
  const profileMut = useMutation({
    mutationFn: ({ path, value }: { path: string; value: string }) =>
      api.patchConfig(path, value),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
    onError: () => setSaveError(i18nT('pages.settings.chatPanel.failed_to_save_profile')),
  })

  // ── Prevent sleep while running (server-side; gateway-host behavior) ──
  const preventSleep = mcCfg?.dashboard?.prevent_sleep ?? false
  const preventSleepMut = useMutation({
    mutationFn: (v: boolean) => api.patchConfig('dashboard.prevent_sleep', v),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
    onError: () => setSaveError(i18nT('pages.settings.chatPanel.failed_to_save_dashboard_config')),
  })

  // ── Session summaries (server-side; spends tokens per changed turn) ──
  const summaryEnabled = mcCfg?.session_summary?.enabled ?? false
  const summaryMut = useMutation({
    mutationFn: (v: boolean) => api.patchConfig('session_summary.enabled', v),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
    onError: () => setSaveError(i18nT('pages.settings.chatPanel.failed_to_save_session_summaries')),
  })

  // "Other" reveals a free-text role. Typed locally and committed on blur /
  // Enter so a PATCH does not fire per keystroke; seeded from the server once
  // the config query resolves, and re-seeded whenever the server value changes
  // (another tab, or the onboarding replay writing it).
  const [localRoleOther, setLocalRoleOther] = useState(userRoleOther)
  const roleOtherSeedRef = useRef(userRoleOther)
  useEffect(() => {
    if (roleOtherSeedRef.current !== userRoleOther) {
      roleOtherSeedRef.current = userRoleOther
      setLocalRoleOther(userRoleOther)
    }
  }, [userRoleOther])
  const commitRoleOther = () => {
    const next = clampRoleOther(localRoleOther)
    if (next === userRoleOther) return
    roleOtherSeedRef.current = next
    setLocalRoleOther(next)
    profileMut.mutate({ path: 'dashboard.user_role_other', value: next })
  }

  const [localBudget, setLocalBudget] = useState('')
  const budgetInitRef = useRef(false)
  useEffect(() => {
    if (mcQ.data && !budgetInitRef.current) {
      budgetInitRef.current = true
      setLocalBudget(String(mcQ.data.agent?.soft_stop_budget_secs ?? SOFT_STOP_DEFAULT))
    }
  }, [mcQ.data])

  const budgetMut = useMutation({
    mutationFn: (n: number) => api.patchConfig('agent.soft_stop_budget_secs', n),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
    onError: () => {
      setSaveError(i18nT('pages.settings.chatPanel.failed_to_save_soft_stop_budget'))
      // Revert the input to the last-known server value so the user isn't
      // left looking at an unpersisted number. budgetInitRef stays true,
      // so the init effect will not clobber this on future query updates.
      setLocalBudget(String(mcCfg?.agent?.soft_stop_budget_secs ?? SOFT_STOP_DEFAULT))
    },
  })

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
  const fallbackModelLabels = (opts: string[]): string[] =>
    opts.map(m =>
      m === ''
        ? i18nT('pages.settings.chatPanel.fallback_disabled')
        : m === 'auto'
          ? i18nT('pages.settings.chatPanel.fallback_auto')
          : m,
    )

  const [localKeepChars, setLocalKeepChars] = useState('')
  const keepCharsInitRef = useRef(false)
  useEffect(() => {
    if (mcQ.data && !keepCharsInitRef.current) {
      keepCharsInitRef.current = true
      setLocalKeepChars(String(mcQ.data.agent?.completion_keep_chars ?? COMPLETION_KEEP_CHARS_DEFAULT))
    }
  }, [mcQ.data])

  const keepCharsMut = useMutation({
    mutationFn: (n: number) => api.patchConfig('agent.completion_keep_chars', n),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
    onError: () => {
      setSaveError(i18nT('pages.settings.chatPanel.failed_to_save_completion_keep_characters'))
      setLocalKeepChars(
        String(mcCfg?.agent?.completion_keep_chars ?? COMPLETION_KEEP_CHARS_DEFAULT)
      )
    },
  })

  const keepModeMut = useMutation({
    mutationFn: (v: CompletionKeepMode) => api.patchConfig('agent.completion_keep', v),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
    onError: () => setSaveError(i18nT('pages.settings.chatPanel.failed_to_save_completion_keep_mode')),
  })

  // ── Default model + default reasoning effort ──
  // These are the DEFAULTS for new sessions. A session's own model/effort
  // picker still overrides them per-slot; nothing here touches live sessions.
  // Same query key as every other model picker so the list is fetched once.
  const availableModels = useAvailableModels()
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

  const defaultModelMut = useMutation(
    optimisticConfigOpts('agent.model', () => i18nT('pages.settings.chatPanel.failed_to_save_default_model'))
  )

  const defaultEffort = mcCfg?.agent?.reasoning_effort ?? ''
  const shownDefaultEffort = overlay.shown('agent.reasoning_effort', defaultEffort)
  // Effort is only meaningful on reasoning-capable models. Rather than hide the
  // row (which would make the setting look absent), keep it visible and
  // disabled with an explanatory hint. Gated on the SHOWN model so the row's
  // enabled state tracks the trigger the user is looking at, not a value the
  // refetch has yet to replace.
  const effortSupported = modelSupportsEffort(shownDefaultModel)
  const defaultEffortMut = useMutation(
    optimisticConfigOpts('agent.reasoning_effort', () =>
      i18nT('pages.settings.chatPanel.failed_to_save_default_reasoning_effort')
    )
  )

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
  // One array per row, shared by `options` and `optionLabels`: SettingsSelect
  // pairs a label to a value by INDEX, so both props must read the same list.
  const backgroundModelOpts = roleModelOptions(shownBackgroundModel, backgroundModel)
  const subagentModelOpts = roleModelOptions(shownSubagentModel, subagentModel)
  const fallbackOpts = fallbackModelOptions(shownFallbackModel, fallbackModel)
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
  const backgroundEffortMut = useMutation(
    optimisticConfigOpts('agent.role_efforts.background', () => i18nT('pages.settings.chatPanel.failed_to_save_role_effort'))
  )
  const subagentEffortMut = useMutation(
    optimisticConfigOpts('agent.role_efforts.subagent', () => i18nT('pages.settings.chatPanel.failed_to_save_role_effort'))
  )

  // ── Local chat config (localStorage) ──
  const setChat = useCallback(<K extends keyof ChatConfig>(k: K, v: ChatConfig[K]) => {
    setChatCfg(prev => {
      const next = { ...prev, [k]: v }
      saveChatConfig(next)
      return next
    })
  }, [])

  const setDash = (patch: Partial<DashboardConfig>) => {
    dashMut.mutate(patch)
  }

  const dashDisabled = !dashQ.isSuccess

  return (
    <>
      <ErrorNotice message={saveError} onDismiss={() => setSaveError('')} className="mb-4 animate-rise" />
      {dashQ.isError && (
        <div className="mb-4 text-[13px] text-danger">
          {i18nT('pages.settings.chatPanel.failed_to_load_dashboard_config')}{' '}
          <button className="underline cursor-pointer bg-transparent border-none text-danger" onClick={() => dashQ.refetch()}>{i18nT('pages.settings.chatPanel.retry')}</button>
        </div>
      )}
      {mcQ.isError && (
        <div className="mb-4 text-[13px] text-danger">
          {i18nT('pages.settings.chatPanel.failed_to_load_config')}{' '}
          <button className="underline cursor-pointer bg-transparent border-none text-danger" onClick={() => mcQ.refetch()}>{i18nT('pages.settings.chatPanel.retry')}</button>
        </div>
      )}

      <SettingsSection title={i18nT('pages.settings.chatPanel.model')}>
        {/* Grouped by role so each block reads as "which model + how hard it
            thinks" for one kind of work, rather than six stacked selects.
            Chat is the interactive default; Background and Sub-agents inherit it
            when left on Auto. */}
        <SettingsCard>
          <div className="text-[13px] font-semibold text-text-strong">{i18nT('pages.settings.chatPanel.role_chat')}</div>
          <SettingsSelect
            label={i18nT('pages.settings.chatPanel.default_model')}
            description={i18nT('pages.settings.chatPanel.which_model_new_sessions_start_with_pick_a_model')}
            hint={i18nT('pages.settings.chatPanel.default_defers_to_your_agent_config_and_then_to')}
            value={shownDefaultModel}
            options={modelOptions}
            optionLabels={modelOptions.map(m => (m === 'auto' ? i18nT('pages.settings.chatPanel.default_auto') : m))}
            onChange={v => defaultModelMut.mutate(v)}
            disabled={!mcQ.isSuccess}
          />
          <SettingsSelect
            label={i18nT('pages.settings.chatPanel.default_reasoning_effort')}
            description={i18nT('pages.settings.chatPanel.how_long_models_think_before_answering_by_defaul')}
            hint={
              effortSupported
                ? i18nT('pages.settings.chatPanel.model_default_applies_no_override_the_model_pick')
                : i18nT('pages.settings.chatPanel.effort_needs_reasoning_model')
            }
            value={shownDefaultEffort}
            options={[...EFFORT_LEVELS]}
            optionLabels={effortLabels}
            onChange={v => defaultEffortMut.mutate(v)}
            disabled={!mcQ.isSuccess || !effortSupported}
          />
        </SettingsCard>

        <SettingsCard index={1}>
          <div className="text-[13px] font-semibold text-text-strong">{i18nT('pages.settings.chatPanel.role_background')}</div>
          <div className="text-[12px] text-muted -mt-0.5">{i18nT('pages.settings.chatPanel.model_for_background_lite_heartbeat_work')}</div>
          <SettingsSelect
            label={i18nT('pages.settings.chatPanel.background_model')}
            hint={i18nT('pages.settings.chatPanel.role_model_auto_hint')}
            value={shownBackgroundModel}
            options={backgroundModelOpts}
            optionLabels={roleModelLabels(backgroundModelOpts)}
            onChange={v => backgroundModelMut.mutate(v)}
            disabled={!mcQ.isSuccess}
          />
          <SettingsSelect
            label={i18nT('pages.settings.chatPanel.background_effort')}
            hint={i18nT('pages.settings.chatPanel.role_effort_hint')}
            value={shownBackgroundEffort}
            options={[...EFFORT_LEVELS]}
            optionLabels={effortLabels}
            onChange={v => backgroundEffortMut.mutate(v)}
            disabled={!mcQ.isSuccess || !bgEffortSupported}
          />
        </SettingsCard>

        <SettingsCard index={2}>
          <div className="text-[13px] font-semibold text-text-strong">{i18nT('pages.settings.chatPanel.role_subagents')}</div>
          <div className="text-[12px] text-muted -mt-0.5">{i18nT('pages.settings.chatPanel.model_for_spawned_sub_agents')}</div>
          <SettingsSelect
            label={i18nT('pages.settings.chatPanel.subagent_model')}
            hint={i18nT('pages.settings.chatPanel.role_model_auto_hint')}
            value={shownSubagentModel}
            options={subagentModelOpts}
            optionLabels={roleModelLabels(subagentModelOpts)}
            onChange={v => subagentModelMut.mutate(v)}
            disabled={!mcQ.isSuccess}
          />
          <SettingsSelect
            label={i18nT('pages.settings.chatPanel.subagent_effort')}
            hint={i18nT('pages.settings.chatPanel.role_effort_hint')}
            value={shownSubagentEffort}
            options={[...EFFORT_LEVELS]}
            optionLabels={effortLabels}
            onChange={v => subagentEffortMut.mutate(v)}
            disabled={!mcQ.isSuccess || !subEffortSupported}
          />
        </SettingsCard>

        <SettingsCard>
          <div className="text-[13px] font-semibold text-text-strong">{i18nT('pages.settings.chatPanel.throttle_fallback')}</div>
          <div className="text-[12px] text-muted -mt-0.5">{i18nT('pages.settings.chatPanel.model_tried_when_your_current_model_stays_rate_li')}</div>
          <SettingsSelect
            label={i18nT('pages.settings.chatPanel.fallback_model')}
            hint={i18nT('pages.settings.chatPanel.fallback_auto_hint')}
            value={shownFallbackModel}
            options={fallbackOpts}
            optionLabels={fallbackModelLabels(fallbackOpts)}
            onChange={v => fallbackMut.mutate(v)}
            disabled={!mcQ.isSuccess}
            configKey="agent.fallback_model"
          />
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title={i18nT('pages.settings.chatPanel.about_you')}>
        <SettingsCard index={3}>
          <SettingsSelect
            label={i18nT('pages.settings.chatPanel.your_role')}
            description={i18nT('pages.settings.chatPanel.kiro_matches_vocabulary_and_examples_to_your_pro')}
            value={userRole}
            options={ROLE_OPTIONS}
            optionLabels={roleLabels()}
            onChange={v => profileMut.mutate({ path: 'dashboard.user_role', value: v })}
          />
          {userRole === 'other' && (
            <SettingsInput
              label={i18nT('pages.settings.chatPanel.describe_your_role')}
              aria-label={i18nT('pages.settings.chatPanel.describe_your_role')}
              description={i18nT('pages.settings.chatPanel.kiro_quotes_this_back_to_itself_when_calibrating')}
              placeholder={i18nT('pages.settings.chatPanel.e_g_solutions_architect_sre_founder')}
              value={localRoleOther}
              onChange={v => setLocalRoleOther(capRoleOther(v))}
              onBlur={commitRoleOther}
            />
          )}
          <SettingsSelect
            label={i18nT('pages.settings.chatPanel.technical_comfort')}
            description={i18nT('pages.settings.chatPanel.sets_how_deep_explanations_go_plain_language_vs')}
            value={userTechLevel}
            options={TECH_OPTIONS}
            optionLabels={techLabels()}
            onChange={v => profileMut.mutate({ path: 'dashboard.user_technical_level', value: v })}
          />
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title={i18nT('pages.settings.chatPanel.power')}>
        <SettingsCard index={4}>
          <SettingsToggle
            label={i18nT('pages.settings.chatPanel.prevent_sleep_while_running')}
            description={i18nT('pages.settings.chatPanel.keep_your_computer_awake_while_a_task_is_running')}
            checked={preventSleep}
            onChange={v => preventSleepMut.mutate(v)}
            disabled={!mcQ.isSuccess}
            configKey="dashboard.prevent_sleep"
          />
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title={i18nT('pages.settings.chatPanel.composer')}>
        <SettingsCard index={5}>
          <SettingsSelect
            label={i18nT('pages.settings.chatPanel.send_shortcut')}
            description={chatCfg.sendOnEnter === 'enter' ? i18nT('pages.settings.chatPanel.shift_enter_for_newline') : chatCfg.sendOnEnter === 'ctrl-enter' ? i18nT('pages.settings.chatPanel.enter_for_newline') : i18nT('pages.settings.chatPanel.mod_enter_for_newline', { mod: isMac ? '⌘' : 'Ctrl' })}
            value={chatCfg.sendOnEnter}
            options={['enter', 'ctrl-enter', 'enter-ctrl-newline']}
            optionLabels={[i18nT('pages.settings.chatPanel.enter_sends'), i18nT('pages.settings.chatPanel.mod_enter_sends', { mod: isMac ? '⌘' : 'Ctrl' }), i18nT('pages.settings.chatPanel.enter_sends_mod_enter_newline', { mod: isMac ? '⌘' : 'Ctrl' })]}
            onChange={v => setChat('sendOnEnter', v as SendMode)}
          />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.quick_send')} description={i18nT('pages.settings.chatPanel.click_a_suggested_reply_to_send_it_instantly', { mod: isMac ? '⇧' : 'Shift' })} checked={dashCfg.quick_send} onChange={v => setDash({ quick_send: v })} disabled={dashDisabled} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.merge_queued_messages')} description={i18nT('pages.settings.chatPanel.combine_follow_up_messages_into_a_single_labeled')} checked={dashCfg.merge_queued_messages} onChange={v => setDash({ merge_queued_messages: v })} disabled={dashDisabled} />
          <SettingsButtonGroup label={i18nT('pages.settings.chatPanel.follow_up_bar_layout')} description={i18nT('pages.settings.chatPanel.multiline_wraps_suggestions_onto_multiple_rows_s')} value={chatCfg.followUpLayout} options={[{ value: "multiline", label: i18nT('pages.settings.chatPanel.multiline') }, { value: "scroll", label: i18nT('pages.settings.chatPanel.single_line') }]} onChange={v => setChat('followUpLayout', v as ChatConfig['followUpLayout'])} />
          <SettingsInput
            label={i18nT('pages.settings.chatPanel.soft_stop_budget_seconds')}
            aria-label={i18nT('pages.settings.chatPanel.soft_stop_budget_seconds')}
            hint={i18nT('pages.settings.chatPanel.how_long_to_wait_for_the_agent_to_honor_a_stop_p')}
            type="number"
            value={localBudget}
            min={SOFT_STOP_MIN}
            max={SOFT_STOP_MAX}
            step={0.5}
            onChange={setLocalBudget}
            onBlur={() => {
              const n = parseFloat(localBudget)
              if (isNaN(n) || n < SOFT_STOP_MIN || n > SOFT_STOP_MAX) {
                setLocalBudget(String(mcCfg?.agent?.soft_stop_budget_secs ?? SOFT_STOP_DEFAULT))
                return
              }
              budgetMut.mutate(n)
            }}
            disabled={!mcQ.isSuccess}
          />
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title={i18nT('pages.settings.chatPanel.messages')}>
        <SettingsCard index={6}>
          <SettingsButtonGroup
            label={i18nT('pages.settings.chatPanel.text_streaming_style')}
            description={i18nT('pages.settings.chatPanel.immediate_mode_shows_raw_chunks_as_they_arrive_s')}
            value={chatCfg.streamMode}
            options={[{ value: 'immediate', label: i18nT('pages.settings.chatPanel.immediate') }, { value: 'smooth', label: i18nT('pages.settings.chatPanel.smooth') }]}
            onChange={v => setChat('streamMode', v as ChatConfig['streamMode'])}
          />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.show_timestamps')} description={i18nT('pages.settings.chatPanel.display_time_on_each_message')} checked={chatCfg.showTimestamps} onChange={v => setChat('showTimestamps', v)} />
          <SettingsButtonGroup label={i18nT('pages.settings.chatPanel.content_width')} description={i18nT('pages.settings.chatPanel.compact_is_the_original_view_comfortable_and_ful')} value={chatCfg.contentWidth} options={[{ value: "compact", label: i18nT('pages.settings.chatPanel.compact') }, { value: "comfortable", label: i18nT('pages.settings.chatPanel.comfortable') }, { value: "full", label: i18nT('pages.settings.chatPanel.full') }]} onChange={v => setChat('contentWidth', v as ContentWidth)} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.show_thinking_inline')} description={i18nT('pages.settings.chatPanel.show_intermediate_reasoning_text_between_tool_ca')} checked={!chatCfg.collapseAllSteps} onChange={v => setChat('collapseAllSteps', !v)} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.pin_last_prompt')} description={i18nT('pages.settings.chatPanel.pin_last_prompt_desc')} checked={chatCfg.pinLastPrompt} onChange={v => {
            setChat('pinLastPrompt', v)
            // Enabling promises the sticky banner this description names; a minimized
            // flag stored earlier would deliver the chip and read as a broken toggle.
            if (v) setChat('pinPromptMinimized', false)
          }} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.simplified_tool_call_names')} description={i18nT('pages.settings.chatPanel.when_enabled_inline_tool_pills_show_simplified_t')} checked={chatCfg.simplifiedToolNames} onChange={v => setChat('simplifiedToolNames', v)} />
          <SettingsSelect label={i18nT('pages.settings.chatPanel.file_change_chips')} description={i18nT('pages.settings.chatPanel.how_file_diff_chips_appear_below_assistant_messa')} value={chatCfg.fileChipStyle} options={['expanded', 'minimal']} optionLabels={[i18nT('pages.settings.chatPanel.expanded_icon_name_stats'), i18nT('pages.settings.chatPanel.minimal_stats_only_name_on_hover')]} onChange={v => setChat('fileChipStyle', v as ChatConfig['fileChipStyle'])} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.link_previews')} description={i18nT('pages.settings.chatPanel.show_a_favicon_and_page_title_instead_of_the_raw')} checked={dashCfg.link_previews} onChange={v => setDash({ link_previews: v })} disabled={dashDisabled} />
          <SettingsSelect label={i18nT('pages.settings.chatPanel.widget_density')} description={i18nT('pages.settings.chatPanel.how_aggressively_the_agent_uses_inline_widgets_f')} value={dashCfg.widget_density ?? 'more'} options={['more', 'less']} optionLabels={[i18nT('pages.settings.chatPanel.more_encourage_widgets'), i18nT('pages.settings.chatPanel.less_only_when_needed')]} onChange={v => setDash({ widget_density: v as 'more' | 'less' })} disabled={dashDisabled} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.mcp_apps_in_side_panel')} description={i18nT('pages.settings.chatPanel.render_interactive_mcp_apps_in_the_right_side_pa')} checked={dashCfg.mcp_app_panel} onChange={v => setDash({ mcp_app_panel: v })} disabled={dashDisabled} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.auto_open_git_panel')} description={i18nT('pages.settings.chatPanel.expand_the_side_panel_to_the_git_tab_each_time_yo')} checked={dashCfg.auto_open_git_panel} onChange={v => setDash({ auto_open_git_panel: v })} disabled={dashDisabled} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.session_card_source_links')} description={i18nT('pages.settings.chatPanel.session_card_source_links_desc')} checked={dashCfg.session_card_source_links} onChange={v => setDash({ session_card_source_links: v })} disabled={dashDisabled} />
          <SettingsSelect label={i18nT('pages.settings.chatPanel.response_verbosity')} description={i18nT('pages.settings.chatPanel.how_terse_the_agent_s_prose_is_ultra_concise_cap')} value={asVerbosity(dashCfg.verbosity)} options={VERBOSITY_OPTIONS} optionLabels={[i18nT('pages.settings.chatPanel.default_normal_length'), i18nT('pages.settings.chatPanel.concise_trim_filler'), i18nT('pages.settings.chatPanel.ultra_concise_3_sentences'), i18nT('pages.settings.chatPanel.answer_only_details_on_request')]} onChange={v => setDash({ verbosity: v as VerbosityLevel })} disabled={dashDisabled} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.show_context_percentage')} description={i18nT('pages.settings.chatPanel.display_usage_percentage_next_to_the_context_pro')} checked={chatCfg.showContextPct} onChange={v => setChat('showContextPct', v)} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.show_token_usage')} description={i18nT('pages.settings.chatPanel.display_used_and_total_tokens_next_to_the_contex')} checked={chatCfg.showContextTokens} onChange={v => setChat('showContextTokens', v)} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.feature_tips')} description={tipsConfigOff ? i18nT('pages.settings.chatPanel.disabled_by_instance_config_tips_enabled_false') : i18nT('pages.settings.chatPanel.show_occasional_feature_discovery_tips_above_the')} checked={!!tipsQ.data && tipsQ.data.enabled_config && !shownOptedOut} onChange={v => tipsMut.mutate(v)} disabled={tipsConfigOff || tipsQ.isLoading || tipsQ.isError} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.folder_suggestions')} description={i18nT('pages.settings.chatPanel.offer_to_file_a_new_session_into_a_matching_fold')} checked={dashCfg.folder_suggestions_enabled} onChange={v => setDash({ folder_suggestions_enabled: v })} disabled={dashDisabled} />
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title={i18nT('pages.settings.chatPanel.sessions')}>
        <SettingsCard index={7}>
          <SettingsToggle label={i18nT('pages.settings.chatPanel.split_view_session_grid')} description={i18nT('pages.settings.chatPanel.opt_in_split_the_chat_into_resizable_session_pan', { mod: isMac ? '⌘' : 'Ctrl' })} checked={dashCfg.session_grid} onChange={v => setDash({ session_grid: v })} disabled={dashDisabled} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.history_expanded')} description={i18nT('pages.settings.chatPanel.expand_history_sidebar_by_default')} checked={chatCfg.historyExpanded} onChange={v => setChat('historyExpanded', v)} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.confirm_before_closing_session')} description={i18nT('pages.settings.chatPanel.show_a_confirmation_dialog_when_closing_a_sessio')} checked={chatCfg.confirmCloseSession} onChange={v => setChat('confirmCloseSession', v)} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.default_to_autopilot_mode')} description={i18nT('pages.settings.chatPanel.new_sessions_start_in_autopilot_mode_plan_approv')} checked={chatCfg.defaultAutopilot} onChange={v => setChat('defaultAutopilot', v)} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.tail_only_fork')} description={i18nT('pages.settings.chatPanel.fork_keeps_only_the_messages_after_the_chosen_po')} checked={dashCfg.tail_fork_enabled} onChange={v => setDash({ tail_fork_enabled: v })} disabled={dashDisabled} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.restore_sessions')} description={i18nT('pages.settings.chatPanel.re_open_recently_active_sessions_on_startup')} checked={dashCfg.restore_sessions} onChange={v => setDash({ restore_sessions: v })} disabled={dashDisabled} />
          {dashCfg.restore_sessions && (
            <SettingsSelect label={i18nT('pages.settings.chatPanel.restore_window')} description={i18nT('pages.settings.chatPanel.time_window_for_session_restoration')} value={String(dashCfg.restore_window_minutes)} options={RESTORE_OPTIONS} optionLabels={restoreLabels()} onChange={v => setDash({ restore_window_minutes: Number(v) })} disabled={dashDisabled} />
          )}
          <SettingsToggle label={i18nT('pages.settings.chatPanel.session_summaries')} description={i18nT('pages.settings.chatPanel.summarize_each_session_by_intent_in_the_right_pa')} checked={summaryEnabled} onChange={v => summaryMut.mutate(v)} disabled={!mcQ.isSuccess || summaryMut.isPending} />
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title={i18nT('pages.settings.chatPanel.context')}>
        <SettingsCard index={8}>
          <SettingsSelect
            label={i18nT('pages.settings.chatPanel.auto_compact_threshold')}
            description={i18nT('pages.settings.chatPanel.context_usage_at_which_auto_compaction_triggers')}
            value={String(mcCfg?.session?.autocompact_pct ?? 70)}
            options={COMPACT_OPTIONS}
            optionLabels={COMPACT_LABELS}
            onChange={v =>
              api.patchConfig('session.autocompact_pct', Number(v))
                .then(() => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }))
                .catch(() => setSaveError(i18nT('pages.settings.chatPanel.failed_to_save_auto_compact_threshold')))
            }
            disabled={!mcQ.isSuccess}
            configKey="session.autocompact_pct"
          />
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title={i18nT('pages.settings.chatPanel.subagents')}>
        <SettingsCard index={10}>
          <SettingsSelect
            label={i18nT('pages.settings.chatPanel.completion_event_truncation')}
            description={i18nT('pages.settings.chatPanel.which_part_of_a_subagent_s_stream_to_keep_when_i')}
            value={mcCfg?.agent?.completion_keep ?? 'head'}
            options={COMPLETION_KEEP_OPTIONS}
            optionLabels={completionKeepLabels()}
            onChange={v => keepModeMut.mutate(v as CompletionKeepMode)}
            disabled={!mcQ.isSuccess}
          />
          <SettingsInput
            label={i18nT('pages.settings.chatPanel.completion_event_characters')}
            aria-label={i18nT('pages.settings.chatPanel.completion_event_characters_2')}
            hint={i18nT('pages.settings.chatPanel.maximum_characters_retained_in_the_completion_ev', { n: COMPLETION_KEEP_CHARS_DEFAULT })}
            type="number"
            value={localKeepChars}
            min={COMPLETION_KEEP_CHARS_MIN}
            max={COMPLETION_KEEP_CHARS_MAX}
            step={500}
            onChange={setLocalKeepChars}
            onBlur={() => {
              const n = parseInt(localKeepChars, 10)
              if (
                isNaN(n) ||
                n < COMPLETION_KEEP_CHARS_MIN ||
                n > COMPLETION_KEEP_CHARS_MAX
              ) {
                setLocalKeepChars(
                  String(mcCfg?.agent?.completion_keep_chars ?? COMPLETION_KEEP_CHARS_DEFAULT)
                )
                return
              }
              keepCharsMut.mutate(n)
            }}
            disabled={!mcQ.isSuccess}
          />
        </SettingsCard>
      </SettingsSection>

    </>
  )
}
