import { useState, useCallback, useRef, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { SettingsSection, SettingsCard, SettingsToggle, SettingsSelect, SettingsInput, SettingsButtonGroup } from '../../components/settings'
import { loadChatConfig, saveChatConfig, type ChatConfig, type ContentWidth, type DashboardConfig, type SendMode } from '../chat/ChatSettings'
import { api } from '../../api/client'
import { useProvider } from '../../providers'
import { modelListRefetchInterval } from '../../providers/modelListHealth'
import { EFFORT_LEVELS, effortLabel, modelSupportsEffort } from '../../lib/effort'
import { isMac } from '../../utils/platform'

const RESTORE_OPTIONS = ['15', '30', '60', '120', '360', '720', '1440', '0']
const RESTORE_LABELS = ['15m', '30m', '1h', '2h', '6h', '12h', '24h', 'No limit']
const COMPACT_OPTIONS = ['20', '40', '60', '80', '90']
const COMPACT_LABELS = ['20% (aggressive)', '40%', '60%', '80%', '90% (default)']

// About You — slugs shared with onboarding step 2 and context.py's prompt maps.
const ROLE_OPTIONS = ['', 'developer', 'designer', 'product-manager', 'data-ml', 'it-ops', 'other']
const ROLE_LABELS = ['Not set', 'Developer', 'UX Designer', 'Product Manager', 'Data / ML', 'IT / Ops', 'Other']
const TECH_OPTIONS = ['', 'codes', 'somewhat-technical', 'non-technical']
const TECH_LABELS = ['Not set', 'I write code', 'Somewhat', 'Not technical']

const SOFT_STOP_MIN = 0.5
const SOFT_STOP_MAX = 60
const SOFT_STOP_DEFAULT = 10.0

type CompletionKeepMode = 'head' | 'tail' | 'both'
const COMPLETION_KEEP_OPTIONS: CompletionKeepMode[] = ['head', 'tail', 'both']
const COMPLETION_KEEP_LABELS = [
  'Head (preserve start of stream)',
  'Tail (preserve end / final summary)',
  'Both (head + tail with truncation marker)',
]
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
  const dashCfg = dashQ.data ?? { restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false, widget_density: 'more' as const, verbosity: 'default' as const, quick_send: false, session_grid: false, tail_fork_enabled: false }

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
      setSaveError('Failed to save tips preference')
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ['tipsStatus'] })
      // Drop any cached/in-flight tip so a running Chat view can't display a
      // tip fetched before the preference changed (Codex round-6).
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
      setSaveError('Failed to save dashboard config')
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
    dashboard?: { user_role?: string; user_technical_level?: string }
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
  const userTechLevel = mcCfg?.dashboard?.user_technical_level ?? ''
  const profileMut = useMutation({
    mutationFn: ({ path, value }: { path: string; value: string }) =>
      api.patchConfig(path, value),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
    onError: () => setSaveError('Failed to save profile'),
  })

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
      setSaveError('Failed to save soft-stop budget')
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
      setSaveError('Failed to save completion-keep characters')
      setLocalKeepChars(
        String(mcCfg?.agent?.completion_keep_chars ?? COMPLETION_KEEP_CHARS_DEFAULT)
      )
    },
  })

  const keepModeMut = useMutation({
    mutationFn: (v: CompletionKeepMode) => api.patchConfig('agent.completion_keep', v),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
    onError: () => setSaveError('Failed to save completion-keep mode'),
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
    onError: () => setSaveError('Failed to save default model'),
  })

  const defaultEffort = mcCfg?.agent?.reasoning_effort ?? ''
  // Effort is only meaningful on reasoning-capable models. Rather than hide the
  // row (which would make the setting look absent), keep it visible and
  // disabled with an explanatory hint.
  const effortSupported = modelSupportsEffort(defaultModel)
  const defaultEffortMut = useMutation({
    mutationFn: (v: string) => api.patchConfig('agent.reasoning_effort', v),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
    onError: () => setSaveError('Failed to save default reasoning effort'),
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
      {saveError && (
        <div className="mb-4 bg-danger/10 border border-danger/20 rounded-lg p-3 flex items-center justify-between animate-rise">
          <span className="text-[13px] text-danger">{saveError}</span>
          <button className="text-[13px] text-danger hover:text-text cursor-pointer bg-transparent border-none" onClick={() => setSaveError('')}>Dismiss</button>
        </div>
      )}
      {dashQ.isError && (
        <div className="mb-4 text-[13px] text-danger">
          Failed to load dashboard config.{' '}
          <button className="underline cursor-pointer bg-transparent border-none text-danger" onClick={() => dashQ.refetch()}>Retry</button>
        </div>
      )}
      {mcQ.isError && (
        <div className="mb-4 text-[13px] text-danger">
          Failed to load config.{' '}
          <button className="underline cursor-pointer bg-transparent border-none text-danger" onClick={() => mcQ.refetch()}>Retry</button>
        </div>
      )}

      <SettingsSection title="Model">
        <SettingsCard>
          <SettingsSelect
            label="Default Model"
            description="Which model new sessions start with. Pick a model inside a session to override it there."
            hint="'Default' defers to your agent config, and then to Kiro's own default. Changing this does not affect sessions that are already open."
            value={defaultModel}
            options={modelOptions}
            optionLabels={modelOptions.map(m => (m === 'auto' ? 'Default (auto)' : m))}
            onChange={v => defaultModelMut.mutate(v)}
            disabled={!mcQ.isSuccess}
          />
          <SettingsSelect
            label="Default Reasoning Effort"
            description="How long models think before answering by default. Higher is slower and costs more. Only available on reasoning-capable models (Opus, Sonnet, Fable, GPT-5.x)."
            hint={
              effortSupported
                ? "'Model default' applies no override — the model picks its own effort."
                : `Reasoning effort is not available on ${defaultModel}. Choose a reasoning-capable model to set a default.`
            }
            value={defaultEffort}
            options={[...EFFORT_LEVELS]}
            optionLabels={EFFORT_LEVELS.map(l => (l === '' ? 'Model default' : effortLabel(l)))}
            onChange={v => defaultEffortMut.mutate(v)}
            disabled={!mcQ.isSuccess || !effortSupported}
          />
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title="About You">
        <SettingsCard>
          <SettingsSelect
            label="Your Role"
            description="Kiro matches vocabulary and examples to your professional background"
            value={userRole}
            options={ROLE_OPTIONS}
            optionLabels={ROLE_LABELS}
            onChange={v => profileMut.mutate({ path: 'dashboard.user_role', value: v })}
          />
          <SettingsSelect
            label="Technical Comfort"
            description="Sets how deep explanations go — plain language vs. full technical detail"
            value={userTechLevel}
            options={TECH_OPTIONS}
            optionLabels={TECH_LABELS}
            onChange={v => profileMut.mutate({ path: 'dashboard.user_technical_level', value: v })}
          />
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title="Composer">
        <SettingsCard>
          <SettingsSelect
            label="Send shortcut"
            description={chatCfg.sendOnEnter === 'enter' ? 'Shift+Enter for newline' : chatCfg.sendOnEnter === 'ctrl-enter' ? 'Enter for newline' : `${isMac ? '⌘' : 'Ctrl'}+Enter for newline`}
            value={chatCfg.sendOnEnter}
            options={['enter', 'ctrl-enter', 'enter-ctrl-newline']}
            optionLabels={['Enter sends', `${isMac ? '⌘' : 'Ctrl'}+Enter sends`, `Enter sends, ${isMac ? '⌘' : 'Ctrl'}+Enter newline`]}
            onChange={v => setChat('sendOnEnter', v as SendMode)}
          />
          <SettingsToggle label="Quick Send" description={`Click a suggested reply to send it instantly. ${isMac ? '⇧' : 'Shift'}+Click to select multiple.`} checked={dashCfg.quick_send} onChange={v => setDash({ quick_send: v })} disabled={dashDisabled} />
          <SettingsToggle label="Merge Queued Messages" description="Combine follow-up messages into a single labeled prompt while the agent is busy" checked={dashCfg.merge_queued_messages} onChange={v => setDash({ merge_queued_messages: v })} disabled={dashDisabled} />
          <SettingsButtonGroup label="Follow-Up Bar Layout" description="Multiline wraps suggestions onto multiple rows. Single line keeps them on one horizontally-scrollable row." value={chatCfg.followUpLayout} options={[{ value: "multiline", label: "Multiline" }, { value: "scroll", label: "Single line" }]} onChange={v => setChat('followUpLayout', v as ChatConfig['followUpLayout'])} />
          <SettingsInput
            label="Soft-stop budget (seconds)"
            aria-label="Soft-stop budget (seconds)"
            hint="How long to wait for the agent to honor a Stop press before forcefully killing the session. Longer budgets preserve session state more often but make stops feel laggy when agents are stuck in long tool calls."
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

      <SettingsSection title="Messages">
        <SettingsCard>
          <SettingsButtonGroup
            label="Text Streaming Style"
            description="Immediate mode shows raw chunks as they arrive. Smooth mode buffers and fades text in at a steady pace."
            value={chatCfg.streamMode}
            options={[{ value: 'immediate', label: 'Immediate' }, { value: 'smooth', label: 'Smooth' }]}
            onChange={v => setChat('streamMode', v as ChatConfig['streamMode'])}
          />
          <SettingsToggle label="Show Timestamps" description="Display time on each message" checked={chatCfg.showTimestamps} onChange={v => setChat('showTimestamps', v)} />
          <SettingsButtonGroup label="Content Width" description="Compact is the original view. Comfortable and Full use more screen space." value={chatCfg.contentWidth} options={[{ value: "compact", label: "Compact" }, { value: "comfortable", label: "Comfortable" }, { value: "full", label: "Full" }]} onChange={v => setChat('contentWidth', v as ContentWidth)} />
          <SettingsToggle label="Show Thinking Inline" description="Show intermediate reasoning text between tool calls instead of collapsing everything" checked={!chatCfg.collapseAllSteps} onChange={v => setChat('collapseAllSteps', !v)} />
          <SettingsToggle label="Simplified Tool Call Names" description="When enabled, inline tool pills show simplified tool use purpose instead of the exact command being run" checked={chatCfg.simplifiedToolNames} onChange={v => setChat('simplifiedToolNames', v)} />
          <SettingsSelect label="File Change Chips" description="How file diff chips appear below assistant messages" value={chatCfg.fileChipStyle} options={['expanded', 'minimal']} optionLabels={['Expanded (icon + name + stats)', 'Minimal (stats only, name on hover)']} onChange={v => setChat('fileChipStyle', v as ChatConfig['fileChipStyle'])} />
          <SettingsSelect label="Widget Density" description="How aggressively the agent uses inline widgets for visual content" value={dashCfg.widget_density ?? 'more'} options={['more', 'less']} optionLabels={['More (encourage widgets)', 'Less (only when needed)']} onChange={v => setDash({ widget_density: v as 'more' | 'less' })} disabled={dashDisabled} />
          <SettingsToggle label="Concise Responses" description="Trim filler and over-narration: lead with the answer, keep progress notes high-level. Code, commands, and error strings stay verbatim; security warnings and multi-step instructions keep full detail." checked={dashCfg.verbosity === 'concise'} onChange={v => setDash({ verbosity: v ? 'concise' : 'default' })} disabled={dashDisabled} />
          <SettingsToggle label="Show Context Percentage" description="Display usage percentage next to the context progress bar" checked={chatCfg.showContextPct} onChange={v => setChat('showContextPct', v)} />
          <SettingsToggle label="Feature Tips" description={tipsConfigOff ? 'Disabled by instance config (tips_enabled: false)' : 'Show occasional feature discovery tips above the composer while the agent is working'} checked={!!tipsQ.data && tipsQ.data.enabled_config && !tipsQ.data.opted_out} onChange={v => tipsMut.mutate(v)} disabled={tipsConfigOff || tipsQ.isLoading || tipsQ.isError} />
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title="Sessions">
        <SettingsCard>
          <SettingsToggle label="Split View (Session Grid)" description={`Opt-in: split the chat into resizable session panes (${isMac ? '⌘' : 'Ctrl'}+D). Experimental.`} checked={dashCfg.session_grid} onChange={v => setDash({ session_grid: v })} disabled={dashDisabled} />
          <SettingsToggle label="History Expanded" description="Expand history sidebar by default" checked={chatCfg.historyExpanded} onChange={v => setChat('historyExpanded', v)} />
          <SettingsToggle label="Confirm Before Closing Session" description="Show a confirmation dialog when closing a session" checked={chatCfg.confirmCloseSession} onChange={v => setChat('confirmCloseSession', v)} />
          <SettingsToggle label="Default to Autopilot Mode" description="New sessions start in autopilot mode (plan → approve → execute). You can still toggle individual sessions." checked={chatCfg.defaultAutopilot} onChange={v => setChat('defaultAutopilot', v)} />
          <SettingsToggle label="Tail-only Fork" description="Fork keeps only the messages after the chosen point instead of those up to it." checked={dashCfg.tail_fork_enabled} onChange={v => setDash({ tail_fork_enabled: v })} disabled={dashDisabled} />
          <SettingsToggle label="Restore Sessions" description="Re-open recently active sessions on startup" checked={dashCfg.restore_sessions} onChange={v => setDash({ restore_sessions: v })} disabled={dashDisabled} />
          {dashCfg.restore_sessions && (
            <SettingsSelect label="Restore Window" description="Time window for session restoration" value={String(dashCfg.restore_window_minutes)} options={RESTORE_OPTIONS} optionLabels={RESTORE_LABELS} onChange={v => setDash({ restore_window_minutes: Number(v) })} disabled={dashDisabled} />
          )}
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title="Context">
        <SettingsCard>
          <SettingsSelect
            label="Auto-Compact Threshold"
            description="Context usage % at which auto-compaction triggers. Lower = more frequent compaction, longer sessions"
            value={String(mcCfg?.session?.autocompact_pct ?? 90)}
            options={COMPACT_OPTIONS}
            optionLabels={COMPACT_LABELS}
            onChange={v =>
              api.patchConfig('session.autocompact_pct', Number(v))
                .then(() => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }))
                .catch(() => setSaveError('Failed to save auto-compact threshold'))
            }
            disabled={!mcQ.isSuccess}
          />
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title="Subagents">
        <SettingsCard>
          <SettingsSelect
            label="Completion Event Truncation"
            description="Which part of a subagent's stream to keep when injecting its completion event into the parent session. Head preserves the start (default, matches legacy behavior). Tail preserves the final summary. Both keeps a slice from each end with a marker between them."
            value={mcCfg?.agent?.completion_keep ?? 'head'}
            options={COMPLETION_KEEP_OPTIONS}
            optionLabels={COMPLETION_KEEP_LABELS}
            onChange={v => keepModeMut.mutate(v as CompletionKeepMode)}
            disabled={!mcQ.isSuccess}
          />
          <SettingsInput
            label="Completion Event Characters"
            aria-label="Completion event characters"
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
