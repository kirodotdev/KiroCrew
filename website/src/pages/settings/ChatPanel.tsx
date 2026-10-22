import { useState, useCallback, useRef, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Trans } from 'react-i18next'
import { SettingsCard, SettingsToggle, SettingsSelect, SettingsInput, SettingsButtonGroup, SettingsMultiSelect, SettingsStepper } from '../../components/settings'
import { SettingsSubNav, type SubNavItem } from '../../components/SettingsSubNav'
import { Btn } from '../../components/ui'
import { MessageSquare, PenLine, Layers, PanelRight, Bot, UserRound, Sparkles, SlidersHorizontal } from 'lucide-react'
import { usePersistedBool } from '../../hooks/usePersistedBool'
import { loadChatConfig, saveChatConfig, MIN_MESSAGE_FONT_SIZE, MAX_MESSAGE_FONT_SIZE, type ChatConfig, type ContentWidth, type DashboardConfig, type MemoryMode, type SendMode } from '../chat/ChatSettings'
import { api } from '../../api/client'
import { useOptimisticConfigPaths, setConfigPathValue } from './useOptimisticConfigPaths'
import { FieldHint, LinkPatternsEditor, type LinkPatternsDraft } from './ChatPanelLinkPatternsEditor'
import {
  RESTORE_OPTIONS, restoreLabels, COMPACT_OPTIONS, compactLabels, ROLE_OPTIONS, roleLabels,
  TECH_OPTIONS, techLabels, SOFT_STOP_MIN, SOFT_STOP_MAX, SOFT_STOP_DEFAULT, TURN_LIMIT_MIN,
  TURN_LIMIT_MAX, TURN_LIMIT_DEFAULT, TITLE_REFRESH_PATH, TITLE_REFRESH_MIN, TITLE_REFRESH_MAX,
  normalizeTitleRefresh, type CompletionKeepMode, COMPLETION_KEEP_OPTIONS, type VerbosityLevel,
  VERBOSITY_OPTIONS, MEMORY_MODE_OPTIONS, FOLDER_ROOT_EXAMPLE, memoryModeLabels, asMemoryMode,
  asVerbosity, completionKeepLabels, COMPLETION_KEEP_CHARS_MIN, COMPLETION_KEEP_CHARS_MAX,
  COMPLETION_KEEP_CHARS_DEFAULT, type KirocrewConfigShape,
} from './chatPanelOptions'
import { useChatPanelDashboard } from './useChatPanelDashboard'
import { useChatPanelTypedFields } from './useChatPanelTypedFields'
import { chatPanelConfigOpts, useChatPanelFallbackModels, useChatPanelModelDefaults } from './useChatPanelModels'
import { AppModelsSection } from './AppModelsSection'
import { FollowDefaultModelAction } from './FollowDefaultModelAction'
import { KiroHarnessFallbackNote } from './KiroHarnessFallbackNote'
import { DefaultSessionFolderSetting } from './DefaultSessionFolderSetting'
import { usePlainDiff } from '../../hooks/usePlainDiff'
import { useDiffSplit } from '../../hooks/useDiffSplit'
import { EFFORT_LEVELS } from '../../lib/effort'
import { isMac } from '../../utils/platform'
import { readBusySendDefault, setBusySendDefault, type BusySendMode } from '../../components/BusySendButton'
import { platformShortcut } from '../../utils/platform'
import { capRoleOther, clampRoleOther } from '../../lib/userProfile'

import { i18nT } from '../../i18n/t'
import { useProvider } from '../../providers'
import { useModelsAuthRequired } from '../../providers/modelListHealth'
import ErrorNotice from '../../components/ErrorNotice'

export type { LinkPatternRule, LinkPatternsDraft } from './ChatPanelLinkPatternsEditor'
export { LinkPatternsEditor } from './ChatPanelLinkPatternsEditor'

export function ChatPanel({ basePath }: { basePath?: string } = {}) {
  const qc = useQueryClient()
  const provider = useProvider()
  const [chatCfg, setChatCfg] = useState<ChatConfig>(loadChatConfig)
  const [saveError, rawSetSaveError] = useState('')
  // The failure banner is one shared slot written by every save on this panel,
  // so a pick may only auto-clear a failure that came from the SAME picker —
  // its own config path. Clearing more than that (another picker's failure,
  // or a non-picker save's) would dismiss an unresolved error and leave the
  // user believing that setting persisted. The ref records which config path
  // produced the current banner; null = not a picker failure.
  const saveErrorPathRef = useRef<string | null>(null)
  // The save-error banner sits at the top of the panel, above the scroll. A
  // chat-setting toggle lower down that fails to save snaps back AND raises the
  // banner, but the user may see only the snap-back and read it as a broken
  // control (UX Review). So each chat-save failure bumps this tick, and the
  // effect below scrolls the banner into view and moves focus to it — the
  // notice is `role="alert"`, so focusing it also re-announces it. A monotonic
  // counter (not a boolean) re-fires on a SECOND identical failure, which the
  // unchanged `saveError` string alone would not.
  const saveErrorBannerRef = useRef<HTMLDivElement | null>(null)
  const [chatSaveFailTick, setChatSaveFailTick] = useState(0)
  useEffect(() => {
    if (chatSaveFailTick === 0) return
    const el = saveErrorBannerRef.current
    if (!el) return
    el.scrollIntoView({ behavior: 'smooth', block: 'center' })
    el.focus()
  }, [chatSaveFailTick])
  // Outlives the Transcript page, which the rail unmounts on a switch.
  const linkPatternsDraft = useRef<LinkPatternsDraft | null>(null)
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

  const {
    dashQ, dashCfg, shownDefaultMemoryMode, tipsQ, tipsMut, tipsConfigOff, shownOptedOut, fvQ,
    fvFetchMut, fv, featureVideoLine, dashMut, defaultModeMut,
  } = useChatPanelDashboard({ qc, setPathSaveError, clearOwnPathError, overlay })

  // ── KiroCrew config (server-side) ──
  const mcQ = useQuery<KirocrewConfigShape>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  const mcCfg = mcQ.data

  const optimisticConfigOpts = chatPanelConfigOpts(overlay, setPathSaveError, clearOwnPathError)

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
  // Pin Summary has no Summary to pin while they are off, so its row says so
  // in view rather than only in its hint. Shown once the config read answers.
  const pinSummaryNeedsSummaries = !!mcCfg && !summaryEnabled
  const summaryMut = useMutation({
    mutationFn: (v: boolean) => api.patchConfig('session_summary.enabled', v),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['kirocrewConfig'] })
      // The side panel reads the flag from each chat's summary query (its +
      // menu row and Pin Summary), which never goes stale on its own.
      qc.invalidateQueries({ queryKey: ['session-summary'] })
    },
    onError: () => setSaveError(i18nT('pages.settings.chatPanel.failed_to_save_session_summaries')),
  })

  // ── Auto-title refresh cadence (server-side; one background call per refresh) ──
  // The GET serializes the LOADED config, so a hand-edited 2 shows as the 4 that
  // runs. Per-path overlay like the Model selectors, so the stepper moves at click
  // time and a slow save cannot roll back a sibling path. A failure also bumps the
  // banner tick: this row sits low in the Sessions card, below the fold on a short
  // window, and a stepper that only snaps back reads as a broken control.
  const titleRefresh = normalizeTitleRefresh(mcCfg?.dashboard?.title_refresh_every_turns ?? 0)
  const shownTitleRefresh = overlay.shown(TITLE_REFRESH_PATH, titleRefresh)
  const titleRefreshMut = useMutation({
    ...overlay.mutationOpts<number>({
      queryKey: ['kirocrewConfig'],
      mutationFn: (v: number) => api.patchConfig(TITLE_REFRESH_PATH, v),
      path: () => TITLE_REFRESH_PATH,
      displayValue: v => v,
      applyToCache: (cached, v) => setConfigPathValue(cached as KirocrewConfigShape, TITLE_REFRESH_PATH, v),
      onFailure: () => {
        setPathSaveError(TITLE_REFRESH_PATH, i18nT('pages.settings.chatPanel.failed_to_save_title_refresh'))
        setChatSaveFailTick(t => t + 1)
      },
      onSupersede: clearOwnPathError,
    }),
    // Rapid clicks fire one PATCH each; a shared scope runs them in click
    // order, so the last click is the value stored. onMutate still runs at
    // click time, so the readout moves immediately.
    scope: { id: TITLE_REFRESH_PATH },
  })
  const setTitleRefresh = (n: number) => {
    const next = normalizeTitleRefresh(n)
    if (next !== shownTitleRefresh) titleRefreshMut.mutate(next)
  }
  /** What the title-refresh readout shows for 0, the built-in schedule. */
  const titleRefreshDefault = i18nT('pages.settings.chatPanel.title_refresh_default')

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
  // What Enter does while the agent is working, for sessions whose split button
  // was never touched (those keep their own per-slot choice). Persisted by
  // BusySendButton's default writer, not by ChatConfig: the per-slot choice and
  // the default must share one storage family or the fallback chain breaks.
  const [busyDefault, setBusyDefaultState] = useState<BusySendMode>(() => readBusySendDefault())
  const setBusyDefault = (m: BusySendMode) => { setBusySendDefault(m); setBusyDefaultState(m) }
  // The served config seeds the budget row ONCE, in the render that first has
  // it. Setting state during render is React's shape for state derived from a
  // value that just arrived: the component re-renders before anything commits,
  // so the commit that enables the input is the one that shows the stored
  // number. Seeding from an effect instead commits one frame where the input is
  // enabled and empty, which a user can see and a test can land on.
  const [budgetSeeded, setBudgetSeeded] = useState(false)
  if (mcQ.data && !budgetSeeded) {
    setBudgetSeeded(true)
    setLocalBudget(String(mcQ.data.agent?.soft_stop_budget_secs ?? SOFT_STOP_DEFAULT))
  }

  const budgetMut = useMutation({
    mutationFn: (n: number) => api.patchConfig('agent.soft_stop_budget_secs', n),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
    onError: () => {
      setSaveError(i18nT('pages.settings.chatPanel.failed_to_save_soft_stop_budget'))
      // Revert the input to the last-known server value so the user isn't
      // left looking at an unpersisted number. budgetSeeded stays true,
      // so the render-phase seed will not clobber this on future query updates.
      setLocalBudget(String(mcCfg?.agent?.soft_stop_budget_secs ?? SOFT_STOP_DEFAULT))
    },
  })

  const {
    fallbackModel, shownFallbackModel, fallbackMut, fallbackModelLabels, refusalFallbackModel,
    shownRefusalFallbackModel, refusalFallbackMut, refusalFallbackModelLabels,
  } = useChatPanelFallbackModels({ overlay, mcCfg, optimisticConfigOpts })

  const {
    localKeepChars, setLocalKeepChars, keepCharsMut, localTurnLimit, setLocalTurnLimit,
    turnLimitEditedRef, turnLimitMut, localFolderRoot, setLocalFolderRoot, folderRootError,
    setFolderRootError, folderRootSaved, setFolderRootSaved, folderRootErrorId,
    folderRootEditedRef, folderRootIme, folderRootMut, commitFolderRoot, localWaitSecs,
    setLocalWaitSecs, waitClampNotice, setWaitClampNotice, waitSecsEditedRef, waitSecsMut,
    keepModeMut,
  } = useChatPanelTypedFields({ qc, setSaveError, dashQ, mcQ, mcCfg })

  const {
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
  } = useChatPanelModelDefaults({
    qc, setPathSaveError, clearOwnPathError, overlay, dashCfg, mcCfg, optimisticConfigOpts,
    fallbackModel, shownFallbackModel, refusalFallbackModel, shownRefusalFallbackModel,
  })

  // ── Plain diffs (localStorage, browser-local) ──
  // Deliberately NOT server config, unlike every other row in the Messages
  // section: the machine painting the diff is the one spending the CPU, so the
  // choice belongs to this client rather than to the whole instance.
  const [plainDiff, setPlainDiff] = usePlainDiff()
  // Default layout every diff surface opens in (side-by-side vs unified),
  // backed by the same client-local `mc-diff-split` preference the surfaces
  // read. Browser-local for the same reason as plain diffs above.
  const [diffSplit, setDiffSplit] = useDiffSplit()

  // Copy on select lives under its own key, not in `mc-chat-config`: that blob
  // is backed up to the host and restored into every browser of the same
  // owner, and this setting overwrites the clipboard. `mc-chat-copy-on-select`
  // is not in DURABLE_PREF_KEYS, so it stays on this device, like the diff
  // layout toggle above. The transcript reads the same key.
  const [copyOnSelect, setCopyOnSelect] = usePersistedBool('mc-chat-copy-on-select', false)

  // ── Local chat config (localStorage) ──
  const setChat = useCallback(<K extends keyof ChatConfig>(k: K, v: ChatConfig[K]) => {
    setChatCfg(prev => {
      const next = { ...prev, [k]: v }
      // `saveChatConfig` returns false when the write (or its dirty marker) could
      // not be persisted and it rolled back, so nothing was stored (GPT 6.1 F1,
      // errors-use-error-notice). Surface that through the shared ErrorNotice
      // banner and keep the PRIOR value on screen, rather than displaying the
      // un-persisted value as if it saved — a reload would discard it. On
      // success, clear any stale save banner this panel raised.
      if (!saveChatConfig(next)) {
        rawSetSaveError(i18nT('pages.settings.chatPanel.failed_to_save_chat_setting'))
        saveErrorPathRef.current = null
        // Bump the tick so the effect scrolls the banner into view and focuses
        // it: a toggle below the fold otherwise just snaps back silently.
        setChatSaveFailTick(t => t + 1)
        return prev
      }
      if (saveErrorPathRef.current === null) rawSetSaveError('')
      return next
    })
  }, [])

  const setDash = (patch: Partial<DashboardConfig>) => {
    dashMut.mutate(patch)
  }

  const dashDisabled = !dashQ.isSuccess

  // Second-level rail groups. Order = rail order; the first item (Transcript)
  // is what the pane shows by default. Every setting the old single scroll held
  // still lives here — the 21-control Messages card is split across Transcript /
  // Side panel / Discovery, and the three one-control sections (Power, Context,
  // Subagents) fold into Advanced so the rail never lists a header per switch.
  const railItems: SubNavItem[] = [
    { key: 'transcript', label: i18nT('pages.settings.chatPanel.transcript'), icon: <MessageSquare size={16} /> },
    { key: 'composer', label: i18nT('pages.settings.chatPanel.composer'), icon: <PenLine size={16} /> },
    { key: 'sessions', label: i18nT('pages.settings.chatPanel.sessions'), icon: <Layers size={16} /> },
    { key: 'sidepanel', label: i18nT('pages.settings.chatPanel.side_panel'), icon: <PanelRight size={16} /> },
    { key: 'models', label: i18nT('pages.settings.chatPanel.model'), icon: <Bot size={16} /> },
    { key: 'aboutyou', label: i18nT('pages.settings.chatPanel.about_you'), icon: <UserRound size={16} /> },
    // An admin-disabled tip toggle with no video row leaves nothing to change, so
    // the rail drops the page rather than lead to one locked row.
    ...(tipsConfigOff && fvQ.isSuccess && !featureVideoLine ? [] : [
      { key: 'discovery', label: i18nT('pages.settings.chatPanel.discovery'), icon: <Sparkles size={16} /> },
    ]),
    { key: 'advanced', label: i18nT('pages.settings.chatPanel.advanced'), icon: <SlidersHorizontal size={16} /> },
  ]

  // Cross-cutting notices (a save failure, a config-load failure) render in the
  // SubNav banner slot so they stay visible in EVERY group — not only whichever
  // one happened to host them when the page was one scroll.
  // When the model list is degraded because kiro-cli's login expired or is
  // signed out, the MODEL-LIST banner's notice says the one thing that fixes it
  // (run `kiro-cli login`) instead of the generic "Failed to load config." The
  // flag is set where the /api/models error is formed (providers/adapters/acp.ts),
  // so this reads one boolean rather than the refusal code; it feeds only the
  // availableModelsQ banner, since the dashboard/roster reads do not carry it.
  const kiroSignInRequired = useModelsAuthRequired(provider.id)
  const configLoadMessage = kiroSignInRequired
    ? i18nT('pages.settings.chatPanel.failed_to_load_config_kiro_signed_out')
    : i18nT('pages.settings.chatPanel.failed_to_load_config')
  const banner = (
    <>
      {/* No hand-off: `localRoleOther`, `localBudget` and `localKeepChars` are
          this panel's live drafts. A hand-off click blurs the field, which STARTS
          a save — and if that save fails after the navigation has unmounted the
          panel, the typed value is gone with nothing left on screen to say so. */}
      {/* Wrapped so a chat-setting save failure can scroll the banner into
          view and move focus to it (UX Review): a toggle below the fold
          otherwise just snaps back with the notice stranded off-screen.
          tabIndex=-1 makes the wrapper programmatically focusable without
          adding a Tab stop. */}
      <div ref={saveErrorBannerRef} tabIndex={-1} className="outline-none">
        <ErrorNotice message={saveError} onDismiss={() => setSaveError('')} className="mb-4 animate-rise" />
      </div>
      {dashQ.isError && (
        <div className="mb-4 flex flex-wrap items-center gap-3">
          {/* No hand-off: the rest of the panel — and its `localRoleOther` /
              `localBudget` / `localKeepChars` drafts — stays mounted under this
              banner, so the navigation would discard them. Retry is the path. */}
          <ErrorNotice
            className="flex-1 min-w-[16rem]"
            message={i18nT('pages.settings.chatPanel.failed_to_load_dashboard_config')}
          />
          <Btn onClick={() => dashQ.refetch()}>{i18nT('pages.settings.chatPanel.retry')}</Btn>
        </div>
      )}
      {(mcQ.isError || agentsQ.isError || resolvedQ.isError) && (
        <div className="mb-4 flex flex-wrap items-center gap-3">
          {/* No hand-off: same drafts as above share this panel. One row for
              the config, the agent roster and the resolved-model reads: they
              fail the same way to the user, so they share one notice and one
              Retry, which re-asks whichever of them failed. Always the generic
              message: the kiro-cli sign-in flag is set only by the model-list
              fetch, so these reads must not borrow its remedy for a failure of
              their own. */}
          <ErrorNotice
            className="flex-1 min-w-[16rem]"
            message={i18nT('pages.settings.chatPanel.failed_to_load_config')}
          />
          <Btn
            onClick={() => {
              if (mcQ.isError) mcQ.refetch()
              if (agentsQ.isError) agentsQ.refetch()
              if (resolvedQ.isError) resolvedQ.refetch()
            }}
          >
            {i18nT('pages.settings.chatPanel.retry')}
          </Btn>
        </div>
      )}
      {(availableModelsQ.isError || availableModelsQ.isDegraded) && (
        <div className="mb-4 flex flex-wrap items-center gap-3">
          {/* No hand-off: the editable drafts in this panel stay mounted while
              the catalog retry runs; navigating away could discard them. This is
              the one banner whose cause the sign-in flag describes, so it shows
              the "run kiro-cli login" message when the model list is degraded by
              an expired / signed-out login. */}
          <ErrorNotice
            className="flex-1 min-w-[16rem]"
            message={configLoadMessage}
          />
          <Btn onClick={() => availableModelsQ.refetch()}>{i18nT('pages.settings.chatPanel.retry')}</Btn>
        </div>
      )}
    </>
  )

  return (
    <SettingsSubNav
      items={railItems}
      basePath={basePath}
      guideTargetPrefix="settings.sub.chat."
      railWidth={220}
      listLabel={i18nT('pages.settings.chatPanel.rail_label')}
      backLabel={i18nT('settings.tabs.chat.label')}
      banner={banner}
    >
      {active => {
        switch (active) {

        case 'models':
          return (
      <>
        {/* Grouped by role so each block reads as "which model + how hard it
            thinks" for one kind of work, rather than six stacked selects.
            Chat is the interactive default; Background and Sub-agents inherit it
            when left on Auto. Borderless like the other single-purpose pages;
            each role heading marks its own group instead of a box. */}
        <div className="mb-8">
        <SettingsCard>
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
          {showPinNotice && pinAgent && (
            <div
              className="mt-1 mb-3 flex flex-wrap items-center gap-3 text-[13px] text-warn"
              role="status"
              data-testid="agent-model-pin-notice"
            >
              <span className="min-w-0 flex-1 break-words">
                <Trans
                  i18nKey={memberPinOverrides
                    ? 'pages.settings.chatPanel.agent_model_pin_overrides_default'
                    : 'pages.settings.chatPanel.agent_template_pin_overrides_default'}
                  components={{
                    agent: <span className="font-mono">{pinAgentName}</span>,
                    model: <span className="font-mono">{resolvedModel}</span>,
                  }}
                />
              </span>
              {memberPinOverrides && (
                <>
                  <Btn
                    type="button"
                    className="shrink-0"
                    onClick={() => clearAgentPinMut.mutate()}
                    disabled={clearAgentPinMut.isPending || pinReadsInFlight || pinReadsFailed || defaultModelMut.isPending}
                  >
                    {/* Names no model: clearing hands new chats to the next tier
                        down, which may be the agent's template rather than this
                        setting, and the panel cannot know which before it asks. */}
                    {i18nT('pages.settings.chatPanel.remove_the_agents_pin')}
                  </Btn>
                  {/* No hand-off: the panel's `localRoleOther` / `localBudget` /
                      `localKeepChars` drafts stay mounted under this row, so the
                      navigation would discard them. The button above is the retry;
                      `mutate()` resets the error. */}
                  <ErrorNotice
                    variant="inline"
                    className="basis-full"
                    message={clearAgentPinError}
                    testId="agent-model-pin-clear-error"
                  />
                </>
              )}
            </div>
          )}
          <FollowDefaultModelAction />
          {/* Selectable Models: label prefers the backend's native name
              (e.g. Opus 5.5 for the id opus) when it sent one, with the id
              beside it as a monospace hint so the row matches the Default
              Model select and the config file; without a name the label is
              the id. value stays the id -- it is the wire value a toggle
              hides/shows. */}
          <SettingsMultiSelect
            label={i18nT('pages.settings.chatPanel.selectable_models')}
            hint={i18nT('pages.settings.chatPanel.selectable_models_description')}
            options={availableModels.map(model => ({
              value: model.name,
              label: model.displayName ?? model.name,
              hint: model.displayName && model.displayName !== model.name ? model.name : undefined,
              description: model.name === 'auto'
                ? i18nT('pages.settings.chatPanel.auto_always_visible')
                : model.description,
              locked: model.name === 'auto',
            }))}
            selected={selectedModelIds}
            onToggle={toggleVisibleModel}
            bulkActions={[
              {
                label: i18nT('components.multiSelect.select_all'),
                onSelect: selectAllModels,
              },
              {
                label: i18nT('components.multiSelect.deselect_all'),
                onSelect: deselectAllModels,
              },
            ]}
            summary={modelPickerSummary}
            searchPlaceholder={i18nT('pages.settings.chatPanel.search_models')}
            disabled={!dashQ.isSuccess || !availableModelsQ.isSuccess || availableModelsQ.isDegraded}
            configKey="dashboard.model_picker_hidden_models"
            settingId="chat.selectable-models"
          />
          <SettingsSelect
            label={i18nT('pages.settings.chatPanel.default_reasoning_effort')}
            description={i18nT('pages.settings.chatPanel.how_long_models_think_before_answering_by_defaul')}
            hint={
              effortSupported
                ? i18nT('pages.settings.chatPanel.model_default_applies_no_override_the_model_pick')
                : i18nT('pages.settings.chatPanel.effort_default_needs_reasoning_model')
            }
            value={shownDefaultEffort}
            options={[...EFFORT_LEVELS]}
            optionLabels={effortLabels}
            onChange={v => defaultEffortMut.mutate(v)}
            disabled={!mcQ.isSuccess || !effortSupported}
          />
          {/* Same row as the model pin notice above, for the effort the default
              agent starts at instead of this setting. */}
          {showEffortPinNotice && pinAgent && (
            <div
              className="mt-1 mb-3 flex flex-wrap items-center gap-3 text-[13px] text-warn"
              role="status"
              data-testid="agent-effort-pin-notice"
            >
              <span className="min-w-0 flex-1 break-words">
                <Trans
                  i18nKey={memberEffortPinOverrides
                    ? 'pages.settings.chatPanel.agent_effort_pin_overrides_default'
                    : 'pages.settings.chatPanel.agent_role_effort_overrides_default'}
                  components={{
                    agent: <span className="font-mono">{pinAgentName}</span>,
                    // A translated level name, not an id, so not monospace.
                    effort: <span className="font-medium">{resolvedEffortLabel}</span>,
                  }}
                />
              </span>
              {memberEffortPinOverrides && (
                <>
                  <Btn
                    type="button"
                    className="shrink-0"
                    onClick={() => clearAgentEffortPinMut.mutate()}
                    disabled={clearAgentEffortPinMut.isPending || pinReadsInFlight || pinReadsFailed || defaultEffortMut.isPending}
                  >
                    {i18nT('pages.settings.chatPanel.clear_agent_effort_pin')}
                  </Btn>
                  {/* No hand-off, for the same drafts as the model clear above. */}
                  <ErrorNotice
                    variant="inline"
                    className="basis-full"
                    message={clearAgentEffortPinError}
                    testId="agent-effort-pin-clear-error"
                  />
                </>
              )}
            </div>
          )}
        </SettingsCard>
        </div>

        <div className="mb-8">
        <h4 className="text-base font-semibold text-text-strong">{i18nT('pages.settings.chatPanel.role_background')}</h4>
        <div className="text-[12px] text-muted mb-1">{i18nT('pages.settings.chatPanel.model_for_background_lite_heartbeat_work')}</div>
        <SettingsCard index={1}>
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
        </div>

        <div className="mb-8">
        <h4 className="text-base font-semibold text-text-strong">{i18nT('pages.settings.chatPanel.role_subagents')}</h4>
        {/* The memory cost of a pin, at the place the pin is chosen: a pinned
            subagent cannot share its parent chat's runtime. */}
        <div className="text-[12px] text-muted mb-1" data-testid="subagent-pin-runtime-cost">
          {i18nT('pages.settings.chatPanel.subagent_pin_runtime_cost')}
        </div>
        <SettingsCard index={2}>
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
        </div>

        <h4 className="text-base font-semibold text-text-strong mb-2">{i18nT('pages.settings.chatPanel.fallbacks')}</h4>
        <KiroHarnessFallbackNote cfg={mcCfg} />
        <div className="mb-8">
        <h5 className="text-sm font-semibold text-text-strong">{i18nT('pages.settings.chatPanel.throttle_fallback')}</h5>
        <div className="text-[12px] text-muted mb-1">{i18nT('pages.settings.chatPanel.model_tried_when_your_current_model_stays_rate_li')}</div>
        <SettingsCard index={3}>
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
        </div>

        <div>
        <h5 className="text-sm font-semibold text-text-strong">{i18nT('pages.settings.chatPanel.refusal_fallback')}</h5>
        <div className="text-[12px] text-muted mb-1">{i18nT('pages.settings.chatPanel.refusal_fallback_desc')}</div>
        <SettingsCard index={4}>
          <SettingsSelect
            label={i18nT('pages.settings.chatPanel.refusal_fallback_model')}
            hint={i18nT('pages.settings.chatPanel.refusal_fallback_auto_hint')}
            value={shownRefusalFallbackModel}
            options={refusalFallbackOpts}
            optionLabels={refusalFallbackModelLabels(refusalFallbackOpts)}
            onChange={v => refusalFallbackMut.mutate(v)}
            disabled={!mcQ.isSuccess}
            configKey="agent.refusal_fallback_model"
          />
        </SettingsCard>
        </div>
        <AppModelsSection index={5} />
      </>
          )

        case 'aboutyou':
          return (
        <SettingsCard>
          <SettingsSelect
            label={i18nT('pages.settings.chatPanel.your_role')}
            hint={i18nT('pages.settings.chatPanel.kiro_matches_vocabulary_and_examples_to_your_pro')}
            value={userRole}
            options={ROLE_OPTIONS}
            optionLabels={roleLabels()}
            onChange={v => profileMut.mutate({ path: 'dashboard.user_role', value: v })}
          />
          {userRole === 'other' && (
            <SettingsInput
              label={i18nT('pages.settings.chatPanel.describe_your_role')}
              aria-label={i18nT('pages.settings.chatPanel.describe_your_role')}
              hint={i18nT('pages.settings.chatPanel.kiro_quotes_this_back_to_itself_when_calibrating')}
              placeholder={i18nT('pages.settings.chatPanel.e_g_solutions_architect_sre_founder')}
              value={localRoleOther}
              onChange={v => setLocalRoleOther(capRoleOther(v))}
              onBlur={commitRoleOther}
            />
          )}
          <SettingsSelect
            label={i18nT('pages.settings.chatPanel.technical_comfort')}
            hint={i18nT('pages.settings.chatPanel.sets_how_deep_explanations_go_plain_language_vs')}
            value={userTechLevel}
            options={TECH_OPTIONS}
            optionLabels={techLabels()}
            onChange={v => profileMut.mutate({ path: 'dashboard.user_technical_level', value: v })}
          />
        </SettingsCard>
          )

        case 'advanced':
          return (
      <>
      {/* Same treatment as Model: headings and spacing mark the groups, no boxes. */}
      <div className="mb-8">
        <h4 className="text-base font-semibold text-text-strong mb-1">{i18nT('pages.settings.chatPanel.power')}</h4>
        <SettingsCard>
          <SettingsToggle
            label={i18nT('pages.settings.chatPanel.prevent_sleep_while_running')}
            hint={i18nT('pages.settings.chatPanel.keep_your_computer_awake_while_a_task_is_running')}
            checked={preventSleep}
            onChange={v => preventSleepMut.mutate(v)}
            disabled={!mcQ.isSuccess}
            configKey="dashboard.prevent_sleep"
          />
        </SettingsCard>
      </div>

      <div className="mb-8">
        <h4 className="text-base font-semibold text-text-strong mb-1">{i18nT('pages.settings.chatPanel.turns')}</h4>
        <SettingsCard index={1}>
          <SettingsInput
            label={i18nT('pages.settings.chatPanel.turn_time_limit')}
            description={i18nT('pages.settings.chatPanel.turn_time_limit_description')}
            hint={i18nT('pages.settings.chatPanel.turn_time_limit_hint')}
            type="number"
            value={localTurnLimit}
            min={TURN_LIMIT_MIN}
            max={TURN_LIMIT_MAX}
            step={300}
            onChange={v => {
              turnLimitEditedRef.current = true
              setLocalTurnLimit(v)
            }}
            onBlur={() => {
              if (!turnLimitEditedRef.current) return
              turnLimitEditedRef.current = false
              const saved = mcCfg?.agent?.chat_turn_timeout_secs ?? TURN_LIMIT_DEFAULT
              const n = Number(localTurnLimit)
              if (localTurnLimit.trim() === '' || !Number.isInteger(n) || n < TURN_LIMIT_MIN || n > TURN_LIMIT_MAX) {
                setLocalTurnLimit(String(saved))
                return
              }
              if (n === saved) return
              turnLimitMut.mutate(n)
            }}
            disabled={!mcQ.isSuccess || turnLimitMut.isPending}
            configKey="agent.chat_turn_timeout_secs"
          />
        </SettingsCard>
      </div>

      <div className="mb-8">
        <h4 className="text-base font-semibold text-text-strong mb-1">{i18nT('pages.settings.chatPanel.context')}</h4>
        <SettingsCard index={2}>
          <SettingsSelect
            label={i18nT('pages.settings.chatPanel.auto_compact_threshold')}
            hint={i18nT('pages.settings.chatPanel.context_usage_at_which_auto_compaction_triggers')}
            value={String(mcCfg?.session?.autocompact_pct ?? 70)}
            options={COMPACT_OPTIONS}
            optionLabels={compactLabels()}
            onChange={v =>
              api.patchConfig('session.autocompact_pct', Number(v))
                .then(() => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }))
                .catch(() => setSaveError(i18nT('pages.settings.chatPanel.failed_to_save_auto_compact_threshold')))
            }
            disabled={!mcQ.isSuccess}
            configKey="session.autocompact_pct"
          />
          <SettingsInput
            label={i18nT('pages.settings.chatPanel.compaction_wait_budget')}
            description={i18nT('pages.settings.chatPanel.compaction_wait_budget_description')}
            hint={i18nT('pages.settings.chatPanel.compaction_wait_budget_hint')}
            type="number"
            value={localWaitSecs}
            min={0}
            max={3600}
            step={30}
            onChange={v => {
              waitSecsEditedRef.current = true
              setLocalWaitSecs(v)
              setWaitClampNotice('')
            }}
            onBlur={() => {
              if (!waitSecsEditedRef.current) return
              waitSecsEditedRef.current = false
              const saved = mcCfg?.session?.compact_wait_secs ?? 0
              const n = Number(localWaitSecs)
              if (localWaitSecs.trim() === '' || !Number.isFinite(n)) {
                setLocalWaitSecs(String(saved))
                return
              }
              if (n === saved) return
              waitSecsMut.mutate(n)
            }}
            // Locked while a save is in flight: its result rewrites the field
            // (the clamped value), which must never land on a newer draft.
            disabled={!mcQ.isSuccess || waitSecsMut.isPending}
            configKey="session.compact_wait_secs"
          />
          <FieldHint message={waitClampNotice} />
        </SettingsCard>
      </div>

      <div>
        <h4 className="text-base font-semibold text-text-strong mb-1">{i18nT('pages.settings.chatPanel.subagents')}</h4>
        <SettingsCard index={3}>
          <SettingsSelect
            label={i18nT('pages.settings.chatPanel.completion_event_truncation')}
            hint={i18nT('pages.settings.chatPanel.which_part_of_a_subagent_s_stream_to_keep_when_i')}
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
      </div>
      </>
          )

        case 'composer':
          return (
        <SettingsCard>
          <SettingsSelect
            label={i18nT('pages.settings.chatPanel.send_shortcut')}
            description={chatCfg.sendOnEnter === 'enter' ? i18nT('pages.settings.chatPanel.shift_enter_for_newline') : chatCfg.sendOnEnter === 'ctrl-enter' ? i18nT('pages.settings.chatPanel.enter_for_newline') : i18nT('pages.settings.chatPanel.mod_enter_for_newline', { mod: isMac ? '⌘' : 'Ctrl' })}
            value={chatCfg.sendOnEnter}
            options={['enter', 'ctrl-enter', 'enter-ctrl-newline']}
            optionLabels={[i18nT('pages.settings.chatPanel.enter_sends'), i18nT('pages.settings.chatPanel.mod_enter_sends', { mod: isMac ? '⌘' : 'Ctrl' }), i18nT('pages.settings.chatPanel.enter_sends_mod_enter_newline', { mod: isMac ? '⌘' : 'Ctrl' })]}
            onChange={v => setChat('sendOnEnter', v as SendMode)}
          />
          <SettingsButtonGroup
            label={i18nT('pages.settings.chatPanel.what_enter_does_while_the_agent_is_working')}
            description={i18nT('pages.settings.chatPanel.busy_alt_action_summary')}
            hint={chatCfg.sendOnEnter === 'enter'
              ? i18nT('pages.settings.chatPanel.busy_alt_action_desc', { chord: platformShortcut('Cmd+Enter') })
              : i18nT('pages.settings.chatPanel.busy_alt_action_desc_no_chord')}
            value={busyDefault}
            options={[
              { value: 'steer', label: i18nT('components.chatInput.steer') },
              { value: 'queue', label: i18nT('components.chatInput.queue') },
            ]}
            onChange={v => setBusyDefault(v as BusySendMode)}
          />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.quick_send')} hint={i18nT('pages.settings.chatPanel.click_a_suggested_reply_to_send_it_instantly', { mod: isMac ? '⇧' : 'Shift' })} checked={dashCfg.quick_send} onChange={v => setDash({ quick_send: v })} disabled={dashDisabled} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.merge_queued_messages')} hint={i18nT('pages.settings.chatPanel.combine_follow_up_messages_into_a_single_labeled')} checked={dashCfg.merge_queued_messages} onChange={v => setDash({ merge_queued_messages: v })} disabled={dashDisabled} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.spellcheck_input')} hint={i18nT('pages.settings.chatPanel.spellcheck_input_desc')} checked={chatCfg.spellcheck} onChange={v => setChat('spellcheck', v)} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.inline_markdown_input')} hint={i18nT('pages.settings.chatPanel.inline_markdown_input_desc')} checked={chatCfg.inlineMarkdown} onChange={v => setChat('inlineMarkdown', v)} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.show_pasted_text_in_full')} description={i18nT('pages.settings.chatPanel.show_pasted_text_in_full_desc', { chord: platformShortcut('Cmd+Shift+V') })} checked={chatCfg.showFullPastes} onChange={v => setChat('showFullPastes', v)} />
          <SettingsButtonGroup label={i18nT('pages.settings.chatPanel.follow_up_bar_layout')} hint={i18nT('pages.settings.chatPanel.multiline_wraps_suggestions_onto_multiple_rows_s')} value={chatCfg.followUpLayout} options={[{ value: "multiline", label: i18nT('pages.settings.chatPanel.multiline') }, { value: "scroll", label: i18nT('pages.settings.chatPanel.single_line') }]} onChange={v => setChat('followUpLayout', v as ChatConfig['followUpLayout'])} />
          <SettingsInput
            label={i18nT('pages.settings.chatPanel.soft_stop_budget_seconds')}
            aria-label={i18nT('pages.settings.chatPanel.soft_stop_budget_seconds')}
            description={i18nT('pages.settings.chatPanel.how_long_to_wait_for_the_agent_to_honor_a_stop_p')}
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
          )

        case 'transcript':
          return (
        <SettingsCard>
          <SettingsButtonGroup
            label={i18nT('pages.settings.chatPanel.text_streaming_style')}
            hint={i18nT('pages.settings.chatPanel.immediate_mode_shows_raw_chunks_as_they_arrive_s')}
            value={chatCfg.streamMode}
            options={[{ value: 'immediate', label: i18nT('pages.settings.chatPanel.immediate') }, { value: 'smooth', label: i18nT('pages.settings.chatPanel.smooth') }]}
            onChange={v => setChat('streamMode', v as ChatConfig['streamMode'])}
          />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.show_timestamps')} hint={i18nT('pages.settings.chatPanel.display_time_on_each_message')} checked={chatCfg.showTimestamps} onChange={v => setChat('showTimestamps', v)} />
          {/* Browser-local like the toggles around it, hence no `configKey`.
              Default off: the gesture takes the double-click that otherwise
              selects a word in the bubble (#7908). */}
          <SettingsToggle label={i18nT('pages.settings.chatPanel.double_click_to_edit')} hint={i18nT('pages.settings.chatPanel.double_click_to_edit_desc')} checked={chatCfg.doubleClickToEdit} onChange={v => setChat('doubleClickToEdit', v)} />
          {/* This device only (its own key, not the backed-up chat config): it
              acts on this browser's clipboard. Default off, since it
              overwrites the clipboard on every drag. */}
          <SettingsToggle label={i18nT('pages.settings.chatPanel.copy_on_select')} hint={i18nT('pages.settings.chatPanel.copy_on_select_desc')} checked={copyOnSelect} onChange={setCopyOnSelect} />
          <SettingsButtonGroup label={i18nT('pages.settings.chatPanel.content_width')} hint={i18nT('pages.settings.chatPanel.compact_is_the_original_view_comfortable_and_ful')} value={chatCfg.contentWidth} options={[{ value: "compact", label: i18nT('pages.settings.chatPanel.compact') }, { value: "comfortable", label: i18nT('pages.settings.chatPanel.comfortable') }, { value: "full", label: i18nT('pages.settings.chatPanel.full') }]} onChange={v => setChat('contentWidth', v as ContentWidth)} />
          <SettingsStepper
            label={i18nT('pages.settings.chatPanel.message_font_size')}
            hint={i18nT('pages.settings.chatPanel.message_font_size_desc')}
            value={chatCfg.messageFontSize}
            onIncrement={() => setChat('messageFontSize', Math.min(MAX_MESSAGE_FONT_SIZE, chatCfg.messageFontSize + 1))}
            onDecrement={() => setChat('messageFontSize', Math.max(MIN_MESSAGE_FONT_SIZE, chatCfg.messageFontSize - 1))}
          />
          <SettingsButtonGroup label={i18nT('pages.settings.chatPanel.minimap_location')} hint={i18nT('pages.settings.chatPanel.minimap_location_desc')} value={chatCfg.minimapSide} options={[{ value: "left", label: i18nT('pages.settings.chatPanel.minimap_side_left') }, { value: "right", label: i18nT('pages.settings.chatPanel.minimap_side_right') }]} onChange={v => setChat('minimapSide', v as ChatConfig['minimapSide'])} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.show_thinking_inline')} hint={i18nT('pages.settings.chatPanel.show_intermediate_reasoning_text_between_tool_ca')} checked={!chatCfg.collapseAllSteps} onChange={v => setChat('collapseAllSteps', !v)} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.pin_last_prompt')} hint={i18nT('pages.settings.chatPanel.pin_last_prompt_desc')} checked={chatCfg.pinLastPrompt} onChange={v => setChat('pinLastPrompt', v)} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.simplified_tool_call_names')} hint={i18nT('pages.settings.chatPanel.when_enabled_inline_tool_pills_show_simplified_t')} checked={chatCfg.simplifiedToolNames} onChange={v => setChat('simplifiedToolNames', v)} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.tool_calls_start_expanded')} hint={i18nT('pages.settings.chatPanel.tool_calls_start_expanded_desc')} checked={chatCfg.toolCallsStartExpanded} onChange={v => setChat('toolCallsStartExpanded', v)} />
          <SettingsSelect label={i18nT('pages.settings.chatPanel.file_change_chips')} hint={i18nT('pages.settings.chatPanel.how_file_diff_chips_appear_below_assistant_messa')} value={chatCfg.fileChipStyle} options={['expanded', 'minimal']} optionLabels={[i18nT('pages.settings.chatPanel.expanded_icon_name_stats'), i18nT('pages.settings.chatPanel.minimal_stats_only_name_on_hover')]} onChange={v => setChat('fileChipStyle', v as ChatConfig['fileChipStyle'])} />
          {/* Sits beside File change chips because it governs the same surface —
              how a diff reads in the transcript. Phrased as "plain diffs ON"
              rather than "highlighting OFF" so the switch position matches the
              stored value — no inverted checkbox. Browser-local, hence no
              `configKey`. */}
          <SettingsToggle
            label={i18nT('settings.chat.plainDiff.label')}
            description={i18nT('settings.chat.plainDiff.description')}
            checked={plainDiff}
            onChange={setPlainDiff}
          />
          {/* Sits beside Plain diffs because it governs the same surface -- how a
              diff opens in the transcript. Phrased as "split ON" so the switch
              position matches the stored value. Browser-local, hence no
              `configKey`. */}
          <SettingsToggle
            label={i18nT('settings.chat.diffLayout.label')}
            hint={i18nT('settings.chat.diffLayout.description')}
            checked={diffSplit}
            onChange={setDiffSplit}
          />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.link_previews')} description={i18nT('pages.settings.chatPanel.show_a_favicon_and_page_title_instead_of_the_raw')} checked={dashCfg.link_previews} onChange={v => setDash({ link_previews: v })} disabled={dashDisabled} />
          <LinkPatternsEditor label={i18nT('pages.settings.chatPanel.link_patterns')} hint={i18nT('pages.settings.chatPanel.link_patterns_desc', { placeholder: '{match}' })} configKey="dashboard.link_patterns" rules={dashCfg.link_patterns ?? []} onSave={next => dashMut.mutateAsync({ link_patterns: next })} disabled={dashDisabled} draft={linkPatternsDraft} />
          <SettingsSelect label={i18nT('pages.settings.chatPanel.widget_density')} hint={i18nT('pages.settings.chatPanel.how_aggressively_the_agent_uses_inline_widgets_f')} value={dashCfg.widget_density ?? 'more'} options={['more', 'less']} optionLabels={[i18nT('pages.settings.chatPanel.more_encourage_widgets'), i18nT('pages.settings.chatPanel.less_only_when_needed')]} onChange={v => setDash({ widget_density: v as 'more' | 'less' })} disabled={dashDisabled} />
          <SettingsSelect label={i18nT('pages.settings.chatPanel.response_verbosity')} hint={i18nT('pages.settings.chatPanel.response_verbosity_hint')} value={asVerbosity(dashCfg.verbosity)} options={VERBOSITY_OPTIONS} optionLabels={[i18nT('pages.settings.chatPanel.default_normal_length'), i18nT('pages.settings.chatPanel.concise_trim_filler'), i18nT('pages.settings.chatPanel.answer_only_details_on_request')]} onChange={v => setDash({ verbosity: v as VerbosityLevel })} disabled={dashDisabled} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.show_context_percentage')} hint={i18nT('pages.settings.chatPanel.display_usage_percentage_next_to_the_context_pro')} checked={chatCfg.showContextPct} onChange={v => setChat('showContextPct', v)} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.show_token_usage')} hint={i18nT('pages.settings.chatPanel.display_used_and_total_tokens_next_to_the_contex')} checked={chatCfg.showContextTokens} onChange={v => setChat('showContextTokens', v)} />
        </SettingsCard>
          )

        case 'sidepanel':
          return (
        <SettingsCard>
          <SettingsToggle label={i18nT('pages.settings.chatPanel.mcp_apps_in_side_panel')} description={i18nT('pages.settings.chatPanel.render_interactive_mcp_apps_in_the_right_side_pa')} checked={dashCfg.mcp_app_panel} onChange={v => setDash({ mcp_app_panel: v })} disabled={dashDisabled} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.auto_open_git_panel')} hint={i18nT('pages.settings.chatPanel.expand_the_side_panel_to_the_git_tab_each_time_yo')} checked={dashCfg.auto_open_git_panel} onChange={v => setDash({ auto_open_git_panel: v })} disabled={dashDisabled} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.side_panel_always_open')} hint={i18nT('pages.settings.chatPanel.side_panel_always_open_hint')} configKey="dashboard.side_panel_always_open" checked={dashCfg.side_panel_always_open ?? false} onChange={v => setDash({ side_panel_always_open: v })} disabled={dashDisabled} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.side_panel_pin_summary')} description={pinSummaryNeedsSummaries ? i18nT('pages.settings.chatPanel.side_panel_pin_summary_needs_session_summaries') : undefined} hint={i18nT('pages.settings.chatPanel.side_panel_pin_summary_hint')} configKey="dashboard.side_panel_pin_summary" checked={dashCfg.side_panel_pin_summary ?? false} onChange={v => setDash({ side_panel_pin_summary: v })} disabled={dashDisabled} />
        </SettingsCard>
          )

        case 'discovery':
          return (
        <SettingsCard>
          <SettingsToggle label={i18nT('pages.settings.chatPanel.feature_tips')} description={tipsConfigOff ? i18nT('pages.settings.chatPanel.disabled_by_instance_config_tips_enabled_false') : undefined} hint={i18nT('pages.settings.chatPanel.show_occasional_feature_discovery_tips_above_the')} checked={!!tipsQ.data && tipsQ.data.enabled_config && !shownOptedOut} onChange={v => tipsMut.mutate(v)} disabled={tipsConfigOff || tipsQ.isLoading || tipsQ.isError} />
          {/* A failed status read used to only grey the toggle out, which is
              indistinguishable from the instance-config gate above. Say why.
              No hand-off: this panel's `localRoleOther` / `localBudget` /
              `localKeepChars` drafts would be unmounted by the navigation. */}
          <ErrorNotice
            variant="inline"
            message={tipsQ.isError ? i18nT('pages.settings.chatPanel.failed_to_load_tips_preference') : null}
          />
          {/* Feature-video cache. A READOUT, not a setting: whether the clips play
              at all is `feature_videos.enabled` on the backend, and this row only
              says what is on disk for the current release plus the one action that
              is the user's to take. It sits beside Feature Tips because the two
              are the same discovery surface, and it is absent entirely when the
              feature is off -- a cache count for something that never plays is
              noise.

              The geometry below is `SettingsToggle`'s, copied deliberately rather
              than approximated: `py-1.5`, a `flex-1 min-w-0 mr-4` caption block, a
              13px semibold label and a 12px muted sub-line. This row sits between
              two toggles, and a few pixels of drift in the label's left edge or
              size reads as a foreign component dropped into the list. It is not a
              `SettingsToggle` itself because it writes no config -- the only
              control here is an action. */}
          {featureVideoLine && (
            <div className="flex items-center justify-between py-1.5">
              <div className="flex-1 min-w-0 mr-4">
                <div className="text-[13px] font-semibold text-text">
                  {i18nT('pages.settings.chatPanel.feature_videos')}
                </div>
                <p data-testid="feature-video-status" className="text-[12px] text-muted mt-0.5 mb-0">
                  {featureVideoLine}
                </p>
              </div>
              {/* Hidden, not disabled, when policy forbids downloads: a control
                  whose only outcome is a refusal explains a policy the user
                  cannot act on. Same render-gate posture as the share entry on
                  the startup clip. Disabled only while a fetch is already
                  running, where pressing again would queue the same work twice. */}
              {fv?.download_enabled && (
                <Btn
                  onClick={() => fvFetchMut.mutate()}
                  disabled={fvFetchMut.isPending || !!fv.downloading}
                  aria-busy={fvFetchMut.isPending}
                >
                  {i18nT('pages.settings.chatPanel.feature_videos_download_all')}
                </Btn>
              )}
            </div>
          )}
          {/* Two separate failures, and the user can act on neither by retrying a
              toggle, so each says which half broke. No hand-off: this panel's
              `localRoleOther` / `localBudget` / `localKeepChars` drafts would be
              unmounted by the navigation. */}
          <ErrorNotice
            variant="inline"
            message={
              fvQ.isError ? i18nT('pages.settings.chatPanel.failed_to_load_feature_video_status')
              : fvFetchMut.isError ? i18nT('pages.settings.chatPanel.failed_to_start_feature_video_download')
              : null
            }
          />
        </SettingsCard>
          )

        case 'sessions':
          return (
        <SettingsCard>
          <SettingsToggle label={i18nT('pages.settings.chatPanel.split_view_session_grid')} description={i18nT('pages.settings.chatPanel.opt_in_split_the_chat_into_resizable_session_pan', { mod: isMac ? '⌘' : 'Ctrl' })} checked={dashCfg.session_grid} onChange={v => setDash({ session_grid: v })} disabled={dashDisabled} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.dim_inactive_panes')} description={i18nT('pages.settings.chatPanel.dim_inactive_panes_desc')} checked={chatCfg.dimInactivePanes} onChange={v => setChat('dimInactivePanes', v)} disabled={dashDisabled || !dashCfg.session_grid} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.history_expanded')} hint={i18nT('pages.settings.chatPanel.expand_history_sidebar_by_default')} checked={chatCfg.historyExpanded} onChange={v => setChat('historyExpanded', v)} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.confirm_before_closing_session')} hint={i18nT('pages.settings.chatPanel.show_a_confirmation_dialog_when_closing_a_sessio')} checked={chatCfg.confirmCloseSession} onChange={v => setChat('confirmCloseSession', v)} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.sidebar_click_opens_tab')} hint={i18nT('pages.settings.chatPanel.sidebar_click_opens_tab_desc')} checked={chatCfg.sidebarClickOpensTab} onChange={v => setChat('sidebarClickOpensTab', v)} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.compact_empty_folders')} hint={i18nT('pages.settings.chatPanel.a_folder_with_no_chats_takes_one_row_instead_of')} checked={chatCfg.hideEmptyFolderBody} onChange={v => setChat('hideEmptyFolderBody', v)} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.full_titles_on_scroll')} hint={i18nT('pages.settings.chatPanel.full_titles_on_scroll_description')} checked={chatCfg.fullTitlesOnScroll} onChange={v => setChat('fullTitlesOnScroll', v)} />
          <SettingsSelect
            label={i18nT('settings.chat.defaultMemoryMode.label')}
            description={i18nT('settings.chat.defaultMemoryMode.description')}
            value={asMemoryMode(shownDefaultMemoryMode)}
            options={MEMORY_MODE_OPTIONS}
            optionLabels={memoryModeLabels()}
            onChange={v => defaultModeMut.mutate(v as MemoryMode)}
            disabled={dashDisabled || defaultModeMut.isPending}
            configKey="dashboard.default_memory_mode"
          />
          <SettingsInput
            label={i18nT('pages.settings.chatPanel.session_folder_root')}
            description={i18nT('pages.settings.chatPanel.session_folder_root_desc')}
            hint={i18nT('pages.settings.chatPanel.session_folder_root_hint')}
            value={localFolderRoot}
            placeholder={i18nT('pages.settings.chatPanel.session_folder_root_placeholder', { path: FOLDER_ROOT_EXAMPLE })}
            onChange={v => {
              folderRootEditedRef.current = true
              setFolderRootError(null)
              setFolderRootSaved(false)
              setLocalFolderRoot(v)
            }}
            {...folderRootIme.bindEnter<HTMLInputElement | HTMLTextAreaElement>({ onEnter: commitFolderRoot, onBlur: commitFolderRoot })}
            disabled={dashDisabled || folderRootMut.isPending}
            invalidMessageId={folderRootError ? folderRootErrorId : undefined}
            after={
              <>
                {/* No hand-off: the typed path is an unsaved draft in this field,
                    and leaving the page to ask the agent would discard it. */}
                <ErrorNotice id={folderRootErrorId} variant="inline" message={folderRootError?.message} report={folderRootError?.report} />
                {folderRootSaved && !folderRootError && (
                  <span role="status" className="text-[12px] text-muted">{i18nT('pages.settings.chatPanel.session_folder_root_saved')}</span>
                )}
              </>
            }
            configKey="dashboard.session_folder_root"
          />
          <DefaultSessionFolderSetting disabled={dashDisabled} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.tail_only_fork')} description={i18nT('pages.settings.chatPanel.fork_keeps_only_the_messages_after_the_chosen_po')} checked={dashCfg.tail_fork_enabled} onChange={v => setDash({ tail_fork_enabled: v })} disabled={dashDisabled} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.restore_sessions')} hint={i18nT('pages.settings.chatPanel.re_open_recently_active_sessions_on_startup')} checked={dashCfg.restore_sessions} onChange={v => setDash({ restore_sessions: v })} disabled={dashDisabled} />
          {dashCfg.restore_sessions && (
            <SettingsSelect label={i18nT('pages.settings.chatPanel.restore_window')} hint={i18nT('pages.settings.chatPanel.time_window_for_session_restoration')} value={String(dashCfg.restore_window_minutes)} options={RESTORE_OPTIONS} optionLabels={restoreLabels()} onChange={v => setDash({ restore_window_minutes: Number(v) })} disabled={dashDisabled} />
          )}
          <SettingsToggle label={i18nT('pages.settings.chatPanel.session_summaries')} description={i18nT('pages.settings.chatPanel.summarize_each_session_by_intent_in_the_right_pa')} hint={i18nT('pages.settings.chatPanel.summarize_each_session_by_intent_hint')} checked={summaryEnabled} onChange={v => summaryMut.mutate(v)} disabled={!mcQ.isSuccess || summaryMut.isPending} />
          {/* 0 is the built-in schedule and reads as "Default", so + leaves it
              for the minimum cadence, − at the minimum returns to it, and − at
              Default is disabled: there is nothing below it. A plain +/− stepper
              like Message Font Size, no typed input. */}
          <SettingsStepper
            label={i18nT('pages.settings.chatPanel.title_refresh_every_turns')}
            description={i18nT('pages.settings.chatPanel.title_refresh_every_turns_desc')}
            hint={i18nT('pages.settings.chatPanel.title_refresh_every_turns_hint')}
            value={shownTitleRefresh === 0 ? titleRefreshDefault : shownTitleRefresh}
            onIncrement={() => setTitleRefresh(shownTitleRefresh < TITLE_REFRESH_MIN ? TITLE_REFRESH_MIN : shownTitleRefresh + 1)}
            onDecrement={() => setTitleRefresh(shownTitleRefresh <= TITLE_REFRESH_MIN ? 0 : shownTitleRefresh - 1)}
            disabled={!mcQ.isSuccess}
            decrementDisabled={shownTitleRefresh === 0}
            reserveWidthFor={[titleRefreshDefault, TITLE_REFRESH_MAX]}
            configKey="dashboard.title_refresh_every_turns"
          />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.session_card_source_links')} description={i18nT('pages.settings.chatPanel.session_card_source_links_desc')} checked={dashCfg.session_card_source_links} onChange={v => setDash({ session_card_source_links: v })} disabled={dashDisabled} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.folder_suggestions')} description={i18nT('pages.settings.chatPanel.offer_to_file_a_new_session_into_a_matching_fold')} checked={dashCfg.folder_suggestions_enabled} onChange={v => setDash({ folder_suggestions_enabled: v })} disabled={dashDisabled} />
          <SettingsToggle label={i18nT('pages.settings.chatPanel.topic_tags')} description={i18nT('pages.settings.chatPanel.topic_tags_desc')} checked={dashCfg.topic_tags_enabled ?? false} onChange={v => setDash({ topic_tags_enabled: v })} disabled={dashDisabled} />
        </SettingsCard>
          )

        default:
          return null
        }
      }}
    </SettingsSubNav>
  )
}
