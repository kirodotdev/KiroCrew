import { useState, useCallback, useRef, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { SettingsSection, SettingsCard, SettingsToggle, SettingsSelect, SettingsInput, SettingsButtonGroup } from '../../components/settings'
import { loadChatConfig, saveChatConfig, type ChatConfig, type ContentWidth, type DashboardConfig, type SendMode } from '../chat/ChatSettings'
import { api } from '../../api/client'
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

/**
 * Return a copy of `cfg` with the dot-separated `path` set to `value`,
 * shallow-cloning only the objects along the path. Used on a config PATCH's
 * success to write the ACCEPTED value into the query cache at that one path,
 * so a transiently failed settle-time refetch cannot leave the display on a
 * pre-PATCH value the server no longer holds.
 */
function setConfigValue(cfg: KirocrewConfigShape, path: string, value: unknown): KirocrewConfigShape {
  const keys = path.split('.')
  const next: Record<string, unknown> = { ...cfg }
  let cursor = next
  for (let i = 0; i < keys.length - 1; i++) {
    const child = cursor[keys[i]]
    cursor[keys[i]] = typeof child === 'object' && child !== null ? { ...(child as Record<string, unknown>) } : {}
    cursor = cursor[keys[i]] as Record<string, unknown>
  }
  cursor[keys[keys.length - 1]] = value
  return next as KirocrewConfigShape
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
  const setPickerSaveError = (path: string, msg: string) => {
    saveErrorPathRef.current = path
    rawSetSaveError(msg)
  }

  // ── Optimistic pending values for the model/effort pickers ──
  // Each picker renders `pending ?? server`, so a pick shows in the trigger
  // immediately instead of after the PATCH → invalidate → refetch round-trip.
  // The overlay is keyed by config path, so concurrent picks on different
  // pickers cannot touch each other's display: a failed mutation only stops
  // masking its OWN path, and a settle-time refetch lands in the query cache
  // UNDER every still-pending overlay rather than clobbering it.
  //
  // Ownership is a monotonic token, not the picked value: `setPending`
  // returns the token from `onMutate` (react-query hands it back to
  // `onSettled` as the mutation context), and only the entry's own token may
  // clear it. Guarding on the value instead would let pick A → pick B →
  // pick A on one picker have A₁'s settle clear the entry A₃ owns, flashing
  // the stale cache value while A₃ is still in flight.
  const pendingSeqRef = useRef(0)
  // Latest token per path, readable synchronously from mutation callbacks
  // (state would be a stale closure there): lets a superseded mutation's
  // late failure recognise it no longer owns the path's display.
  const latestTokenRef = useRef<Record<string, number>>({})
  const [pendingCfg, setPendingCfg] = useState<Record<string, { value: string; token: number } | undefined>>({})
  const setPending = (path: string, value: string): number => {
    const token = ++pendingSeqRef.current
    latestTokenRef.current[path] = token
    setPendingCfg(prev => ({ ...prev, [path]: { value, token } }))
    // A fresh attempt supersedes a stale failure banner from THIS picker:
    // without this the trigger would show the new pick while its own last
    // pick's error still hangs above it in the same frame. Failures from any
    // other source — another picker included — stay up: this pick says
    // nothing about whether that other setting saved.
    if (saveErrorPathRef.current === path) setSaveError('')
    return token
  }
  const clearPending = (path: string, token: unknown) =>
    setPendingCfg(prev => {
      // A newer pick on the same path owns the display now; leave it alone.
      if (prev[path]?.token !== token) return prev
      const next = { ...prev }
      delete next[path]
      return next
    })

  // ── Dashboard config (server-side) ──
  const dashQ = useQuery<DashboardConfig>({
    queryKey: ['dashboardConfig'],
    queryFn: () => api.dashboardConfig(),
  })
  const dashCfg = dashQ.data ?? { restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false, widget_density: 'more' as const, verbosity: 'default' as const, quick_send: false, session_grid: false, tail_fork_enabled: false, link_previews: false, mcp_app_panel: false, auto_open_git_panel: false, folder_suggestions_enabled: true, use_builtin_browser: true }

  // ── Feature Tips opt-out (server-side per-user state) ──
  const tipsQ = useQuery<{ enabled_config: boolean; opted_out: boolean }>({
    queryKey: ['tipsStatus'],
    queryFn: () => api.tipsStatus(),
  })
  const tipsMut = useMutation({
    mutationFn: (enable: boolean) => api.tipsFeedback('', enable ? 'optin' : 'optout'),
    onMutate: async (enable) => {
      await qc.cancelQueries({ queryKey: ['tipsStatus'] })
      const prev = qc.getQueryData<{ enabled_config: boolean; opted_out: boolean }>(['tipsStatus'])
      if (prev) qc.setQueryData(['tipsStatus'], { ...prev, opted_out: !enable })
      return { prev }
    },
    onError: (_err, _vars, ctx) => {
      if (ctx?.prev) qc.setQueryData(['tipsStatus'], ctx.prev)
      setSaveError(i18nT('pages.settings.chatPanel.failed_to_save_tips_preference'))
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ['tipsStatus'] })
      // Drop any cached/in-flight tip so a running Chat view can't display a
      // tip fetched before the preference changed.
      qc.removeQueries({ queryKey: ['tips-next'] })
    },
  })
  const tipsConfigOff = tipsQ.data ? !tipsQ.data.enabled_config : false

  const dashMut = useMutation({
    mutationFn: (next: DashboardConfig) => api.updateDashboardConfig(next),
    onMutate: async (next) => {
      await qc.cancelQueries({ queryKey: ['dashboardConfig'] })
      const prev = qc.getQueryData<DashboardConfig>(['dashboardConfig'])
      qc.setQueryData(['dashboardConfig'], next)
      return { prev }
    },
    onError: (_err, _vars, ctx) => {
      if (ctx?.prev) qc.setQueryData(['dashboardConfig'], ctx.prev)
      setSaveError(i18nT('pages.settings.chatPanel.failed_to_save_dashboard_config'))
    },
    onSettled: () => qc.invalidateQueries({ queryKey: ['dashboardConfig'] }),
  })

  // ── KiroCrew config (server-side) ──
  const mcQ = useQuery<KirocrewConfigShape>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  const mcCfg = mcQ.data

  /**
   * Mutation options for a config PATCH with an OPTIMISTIC display: the seven
   * Model-section selectors render `pending ?? server`, so a pick shows
   * immediately instead of waiting for the PATCH + refetch round-trip.
   *
   * The pending entry is per config PATH, never a whole-config snapshot:
   * snapshotting the whole object would capture another picker's in-flight
   * optimistic value and restore it on this mutation's failure, transiently
   * reverting a pick the user never undid. With the overlay, an error only
   * stops masking this mutation's own path.
   *
   * On success the ACCEPTED value is first written into the cache at this
   * path only, so a transiently failed settle-time refetch (which
   * invalidateQueries swallows) cannot leave the display on a pre-PATCH
   * value the server no longer holds. `onSuccess` then RETURNS the
   * invalidateQueries promise — react-query awaits it before `onSettled` —
   * so by the time the pending entry clears, a completed refetch has already
   * replaced that write with the server's authoritative answer (which may
   * differ from the pick when the backend normalises it). The refetch cannot
   * clobber a DIFFERENT picker's in-flight pick, because that picker's
   * overlay still masks the cache until its own settle. On error the cache
   * was never written, but the refetch still runs: a PATCH can fail after
   * persisting (5xx after apply, proxy timeout), and only the server can say
   * which value survived. `''` is a meaningful value here ("model default" /
   * fallback "disabled"), hence `??` over truthiness everywhere the overlay
   * is read.
   *
   * The failure banner is token-guarded: a pick that has been superseded by
   * a newer pick on the same path reports nothing when it eventually fails —
   * the newer pick owns the display, and a stale "failed to save" beside a
   * value that did persist is exactly the co-render this fix removes.
   */
  const optimisticConfigOpts = (path: string, errMsg: (err: unknown) => string) => ({
    mutationFn: (v: string) => api.patchConfig(path, v),
    onMutate: (v: string) => setPending(path, v),
    onSuccess: (_data: unknown, v: string, token: unknown) => {
      // Only the path's LATEST pick may write its accepted value: with A→B
      // in flight on one picker and B settling first, A's later settle would
      // otherwise overwrite B in the cache, and a failed refetch would leave
      // stale A displayed while the server holds B.
      if (latestTokenRef.current[path] === token) {
        const cur = qc.getQueryData<KirocrewConfigShape>(['kirocrewConfig'])
        if (cur) qc.setQueryData(['kirocrewConfig'], setConfigValue(cur, path, v))
      }
      return qc.invalidateQueries({ queryKey: ['kirocrewConfig'] })
    },
    onError: (err: unknown, _v: string, token: unknown) => {
      // Stop masking immediately (token-guarded and idempotent with the
      // onSettled clear) so no intermediate frame shows the failed pick
      // beside its own failure banner.
      clearPending(path, token)
      if (latestTokenRef.current[path] === token) setPickerSaveError(path, errMsg(err))
    },
    onSettled: (_data: unknown, err: unknown, _v: string, token: unknown) => {
      clearPending(path, token)
      if (err) qc.invalidateQueries({ queryKey: ['kirocrewConfig'] })
    },
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
  const shownFallbackModel = pendingCfg['agent.fallback_model']?.value ?? fallbackModel
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
  const shownDefaultModel = pendingCfg['agent.model']?.value ?? defaultModel
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
  const shownDefaultEffort = pendingCfg['agent.reasoning_effort']?.value ?? defaultEffort
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
  const shownBackgroundModel = pendingCfg['agent.role_models.background']?.value ?? backgroundModel
  const shownSubagentModel = pendingCfg['agent.role_models.subagent']?.value ?? subagentModel
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
  const shownBackgroundEffort = pendingCfg['agent.role_efforts.background']?.value ?? backgroundEffort
  const shownSubagentEffort = pendingCfg['agent.role_efforts.subagent']?.value ?? subagentEffort
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
    dashMut.mutate({ ...dashCfg, ...patch })
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
          <SettingsToggle label={i18nT('pages.settings.chatPanel.pin_last_prompt')} description={i18nT('pages.settings.chatPanel.pin_last_prompt_desc')} checked={chatCfg.pinLastPrompt} onChange={v => setChat('pinLastPrompt', v)} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.simplified_tool_call_names')} description={i18nT('pages.settings.chatPanel.when_enabled_inline_tool_pills_show_simplified_t')} checked={chatCfg.simplifiedToolNames} onChange={v => setChat('simplifiedToolNames', v)} />
          <SettingsSelect label={i18nT('pages.settings.chatPanel.file_change_chips')} description={i18nT('pages.settings.chatPanel.how_file_diff_chips_appear_below_assistant_messa')} value={chatCfg.fileChipStyle} options={['expanded', 'minimal']} optionLabels={[i18nT('pages.settings.chatPanel.expanded_icon_name_stats'), i18nT('pages.settings.chatPanel.minimal_stats_only_name_on_hover')]} onChange={v => setChat('fileChipStyle', v as ChatConfig['fileChipStyle'])} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.link_previews')} description={i18nT('pages.settings.chatPanel.show_a_favicon_and_page_title_instead_of_the_raw')} checked={dashCfg.link_previews} onChange={v => setDash({ link_previews: v })} disabled={dashDisabled} />
          <SettingsSelect label={i18nT('pages.settings.chatPanel.widget_density')} description={i18nT('pages.settings.chatPanel.how_aggressively_the_agent_uses_inline_widgets_f')} value={dashCfg.widget_density ?? 'more'} options={['more', 'less']} optionLabels={[i18nT('pages.settings.chatPanel.more_encourage_widgets'), i18nT('pages.settings.chatPanel.less_only_when_needed')]} onChange={v => setDash({ widget_density: v as 'more' | 'less' })} disabled={dashDisabled} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.mcp_apps_in_side_panel')} description={i18nT('pages.settings.chatPanel.render_interactive_mcp_apps_in_the_right_side_pa')} checked={dashCfg.mcp_app_panel} onChange={v => setDash({ mcp_app_panel: v })} disabled={dashDisabled} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.auto_open_git_panel')} description={i18nT('pages.settings.chatPanel.expand_the_side_panel_to_the_git_tab_each_time_yo')} checked={dashCfg.auto_open_git_panel} onChange={v => setDash({ auto_open_git_panel: v })} disabled={dashDisabled} />
          <SettingsSelect label={i18nT('pages.settings.chatPanel.response_verbosity')} description={i18nT('pages.settings.chatPanel.how_terse_the_agent_s_prose_is_ultra_concise_cap')} value={asVerbosity(dashCfg.verbosity)} options={VERBOSITY_OPTIONS} optionLabels={[i18nT('pages.settings.chatPanel.default_normal_length'), i18nT('pages.settings.chatPanel.concise_trim_filler'), i18nT('pages.settings.chatPanel.ultra_concise_3_sentences'), i18nT('pages.settings.chatPanel.answer_only_details_on_request')]} onChange={v => setDash({ verbosity: v as VerbosityLevel })} disabled={dashDisabled} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.show_context_percentage')} description={i18nT('pages.settings.chatPanel.display_usage_percentage_next_to_the_context_pro')} checked={chatCfg.showContextPct} onChange={v => setChat('showContextPct', v)} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.show_token_usage')} description={i18nT('pages.settings.chatPanel.display_used_and_total_tokens_next_to_the_contex')} checked={chatCfg.showContextTokens} onChange={v => setChat('showContextTokens', v)} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.feature_tips')} description={tipsConfigOff ? i18nT('pages.settings.chatPanel.disabled_by_instance_config_tips_enabled_false') : i18nT('pages.settings.chatPanel.show_occasional_feature_discovery_tips_above_the')} checked={!!tipsQ.data && tipsQ.data.enabled_config && !tipsQ.data.opted_out} onChange={v => tipsMut.mutate(v)} disabled={tipsConfigOff || tipsQ.isLoading || tipsQ.isError} />
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
