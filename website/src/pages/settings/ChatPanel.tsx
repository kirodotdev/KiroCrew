import { useState, useCallback, useRef, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { SettingsSection, SettingsCard, SettingsToggle, SettingsSelect, SettingsInput, SettingsButtonGroup } from '../../components/settings'
import { loadChatConfig, saveChatConfig, type ChatConfig, type ContentWidth, type DashboardConfig, type SendMode } from '../chat/ChatSettings'
import { api } from '../../api/client'
import { useProvider } from '../../providers'
import { modelListRefetchInterval } from '../../providers/modelListHealth'
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
  return ['15m', '30m', '1h', '2h', '6h', '12h', '24h', i18nT('pages.chat.chatSettings.no_limit')]
}
const COMPACT_OPTIONS = ['20', '40', '60', '80', '90']
const COMPACT_LABELS = ['20% (aggressive)', '40%', '60%', '80%', '90% (default)']

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

export function ChatPanel() {
  const qc = useQueryClient()
  const provider = useProvider()
  const [chatCfg, setChatCfg] = useState<ChatConfig>(loadChatConfig)
  const [saveError, setSaveError] = useState('')

  // ── Dashboard config (server-side) ──
  const dashQ = useQuery<DashboardConfig>({
    queryKey: ['dashboardConfig'],
    queryFn: () => api.dashboardConfig(),
  })
  const dashCfg = dashQ.data ?? { restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false, widget_density: 'more' as const, verbosity: 'default' as const, quick_send: false, session_grid: false, tail_fork_enabled: false, link_previews: false, mcp_app_panel: false }

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
  const mcQ = useQuery<{
    session?: { autocompact_pct?: number }
    agent?: {
      model?: string
      reasoning_effort?: string
      soft_stop_budget_secs?: number
      completion_keep?: CompletionKeepMode
      completion_keep_chars?: number
    }
    dashboard?: { user_role?: string; user_role_other?: string; user_technical_level?: string }
  }>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  const mcCfg = mcQ.data

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
  const { data: availableModels = [{ name: 'auto', description: 'Default' }] } = useQuery({
    queryKey: ['available-models', provider.id],
    queryFn: async () => {
      const models = await provider.fetchAvailableModels()
      return [{ name: 'auto', description: 'Default' }, ...models.filter(m => m.name !== 'auto')]
    },
    refetchInterval: modelListRefetchInterval,
  })
  // '' in config means "unset" and resolves the same way 'auto' does, so both
  // render as the 'auto' option rather than as a missing selection.
  const defaultModel = mcCfg?.agent?.model || 'auto'
  const modelOptions = availableModels.map(m => m.name)
  // A model the live backend no longer advertises must still be selectable,
  // otherwise the select would silently jump to another entry and a stray
  // change event would overwrite the user's stored choice.
  if (!modelOptions.includes(defaultModel)) modelOptions.unshift(defaultModel)

  const defaultModelMut = useMutation({
    mutationFn: (v: string) => api.patchConfig('agent.model', v),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
    onError: () => setSaveError(i18nT('pages.settings.chatPanel.failed_to_save_default_model')),
  })

  const defaultEffort = mcCfg?.agent?.reasoning_effort ?? ''
  // Effort is only meaningful on reasoning-capable models. Rather than hide the
  // row (which would make the setting look absent), keep it visible and
  // disabled with an explanatory hint.
  const effortSupported = modelSupportsEffort(defaultModel)
  const defaultEffortMut = useMutation({
    mutationFn: (v: string) => api.patchConfig('agent.reasoning_effort', v),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
    onError: () => setSaveError(i18nT('pages.settings.chatPanel.failed_to_save_default_reasoning_effort')),
  })

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
        <SettingsCard>
          <SettingsSelect
            label={i18nT('pages.settings.chatPanel.default_model')}
            description={i18nT('pages.settings.chatPanel.which_model_new_sessions_start_with_pick_a_model')}
            hint={i18nT('pages.settings.chatPanel.default_defers_to_your_agent_config_and_then_to')}
            value={defaultModel}
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
                : `Reasoning effort is not available on ${defaultModel}. Choose a reasoning-capable model to set a default.`
            }
            value={defaultEffort}
            options={[...EFFORT_LEVELS]}
            optionLabels={EFFORT_LEVELS.map(l => (l === '' ? i18nT('pages.settings.chatPanel.model_default') : effortLabel(l)))}
            onChange={v => defaultEffortMut.mutate(v)}
            disabled={!mcQ.isSuccess || !effortSupported}
          />
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title={i18nT('pages.settings.chatPanel.about_you')}>
        <SettingsCard>
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

      <SettingsSection title={i18nT('pages.settings.chatPanel.composer')}>
        <SettingsCard>
          <SettingsSelect
            label={i18nT('pages.settings.chatPanel.send_shortcut')}
            description={chatCfg.sendOnEnter === 'enter' ? i18nT('pages.settings.chatPanel.shift_enter_for_newline') : chatCfg.sendOnEnter === 'ctrl-enter' ? i18nT('pages.settings.chatPanel.enter_for_newline') : i18nT('pages.settings.chatPanel.mod_enter_for_newline', { mod: isMac ? '⌘' : 'Ctrl' })}
            value={chatCfg.sendOnEnter}
            options={['enter', 'ctrl-enter', 'enter-ctrl-newline']}
            optionLabels={[i18nT('pages.settings.chatPanel.enter_sends'), i18nT('pages.settings.chatPanel.mod_enter_sends', { mod: isMac ? '⌘' : 'Ctrl' }), i18nT('pages.settings.chatPanel.enter_sends_mod_enter_newline', { mod: isMac ? '⌘' : 'Ctrl' })]}
            onChange={v => setChat('sendOnEnter', v as SendMode)}
          />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.quick_send')} description={`Click a suggested reply to send it instantly. ${isMac ? '⇧' : 'Shift'}+Click to select multiple.`} checked={dashCfg.quick_send} onChange={v => setDash({ quick_send: v })} disabled={dashDisabled} />
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
        <SettingsCard>
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
          <SettingsToggle label={i18nT('pages.settings.chatPanel.concise_responses')} description={i18nT('pages.settings.chatPanel.trim_filler_and_over_narration_lead_with_the_ans')} checked={dashCfg.verbosity === 'concise'} onChange={v => setDash({ verbosity: v ? 'concise' : 'default' })} disabled={dashDisabled} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.show_context_percentage')} description={i18nT('pages.settings.chatPanel.display_usage_percentage_next_to_the_context_pro')} checked={chatCfg.showContextPct} onChange={v => setChat('showContextPct', v)} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.feature_tips')} description={tipsConfigOff ? i18nT('pages.settings.chatPanel.disabled_by_instance_config_tips_enabled_false') : i18nT('pages.settings.chatPanel.show_occasional_feature_discovery_tips_above_the')} checked={!!tipsQ.data && tipsQ.data.enabled_config && !tipsQ.data.opted_out} onChange={v => tipsMut.mutate(v)} disabled={tipsConfigOff || tipsQ.isLoading || tipsQ.isError} />
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title={i18nT('pages.settings.chatPanel.sessions')}>
        <SettingsCard>
          <SettingsToggle label={i18nT('pages.settings.chatPanel.split_view_session_grid')} description={`Opt-in: split the chat into resizable session panes (${isMac ? '⌘' : 'Ctrl'}+D). Experimental.`} checked={dashCfg.session_grid} onChange={v => setDash({ session_grid: v })} disabled={dashDisabled} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.history_expanded')} description={i18nT('pages.settings.chatPanel.expand_history_sidebar_by_default')} checked={chatCfg.historyExpanded} onChange={v => setChat('historyExpanded', v)} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.confirm_before_closing_session')} description={i18nT('pages.settings.chatPanel.show_a_confirmation_dialog_when_closing_a_sessio')} checked={chatCfg.confirmCloseSession} onChange={v => setChat('confirmCloseSession', v)} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.default_to_autopilot_mode')} description={i18nT('pages.settings.chatPanel.new_sessions_start_in_autopilot_mode_plan_approv')} checked={chatCfg.defaultAutopilot} onChange={v => setChat('defaultAutopilot', v)} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.tail_only_fork')} description={i18nT('pages.settings.chatPanel.fork_keeps_only_the_messages_after_the_chosen_po')} checked={dashCfg.tail_fork_enabled} onChange={v => setDash({ tail_fork_enabled: v })} disabled={dashDisabled} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.restore_sessions')} description={i18nT('pages.settings.chatPanel.re_open_recently_active_sessions_on_startup')} checked={dashCfg.restore_sessions} onChange={v => setDash({ restore_sessions: v })} disabled={dashDisabled} />
          {dashCfg.restore_sessions && (
            <SettingsSelect label={i18nT('pages.settings.chatPanel.restore_window')} description={i18nT('pages.settings.chatPanel.time_window_for_session_restoration')} value={String(dashCfg.restore_window_minutes)} options={RESTORE_OPTIONS} optionLabels={restoreLabels()} onChange={v => setDash({ restore_window_minutes: Number(v) })} disabled={dashDisabled} />
          )}
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title={i18nT('pages.settings.chatPanel.context')}>
        <SettingsCard>
          <SettingsSelect
            label={i18nT('pages.settings.chatPanel.auto_compact_threshold')}
            description={i18nT('pages.settings.chatPanel.context_usage_at_which_auto_compaction_triggers')}
            value={String(mcCfg?.session?.autocompact_pct ?? 90)}
            options={COMPACT_OPTIONS}
            optionLabels={COMPACT_LABELS}
            onChange={v =>
              api.patchConfig('session.autocompact_pct', Number(v))
                .then(() => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }))
                .catch(() => setSaveError(i18nT('pages.settings.chatPanel.failed_to_save_auto_compact_threshold')))
            }
            disabled={!mcQ.isSuccess}
          />
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title={i18nT('pages.settings.chatPanel.subagents')}>
        <SettingsCard>
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
            hint={`Maximum characters retained in the completion event after applying the truncation mode. 0 disables truncation entirely. Default ${COMPLETION_KEEP_CHARS_DEFAULT}.`}
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
